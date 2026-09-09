"""CDP Browser MCP Server.

Exposes browser automation tools via MCP protocol using an in-process
transport.  Each tool delegates to a Playwright connection over the embedded
Qt browser overlay CDP endpoint, so users can watch the agent work live.

Shared safety helpers (URL/SSRF validation, payment-value redaction) live in
``mcp_servers.browser.safety_helpers`` and are used by both this server and the
Playwright browser server.  The ``ai_controller`` registers this CDP server on
desktop surfaces and the Playwright server on cloud/headless surfaces, so the
agent sees the same compact tool surface on either route.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from core.logging_config import get_logger
from mcp_servers.browser.js_safety import blocked_js_pattern
from mcp_servers.browser.safety_helpers import (
    _black_screenshot_payload,
    _http_error_page_payload,
    _is_css_selector,
    _js_payment_value_bypass,
    _json,
    _navigation_fact_fields,
    _normalize_navigation_url,
    _payment_value_violation,
    _truncate,
    _validate_url,
    detect_page_health,
)
from mcp_servers.browser.server import (
    _JS_SIGNATURE_ACTION_RE,
    _ORDER_SUBMIT_RE,
    _PAYMENT_FIELD_KEYWORDS,
    _SIGNATURE_ACTION_MARKERS,
    _SIGNATURE_PAGE_MARKERS,
    _SIGNATURE_URL_RE,
    _browser_eval_error_text,
    _consume_payment_gate_override,
    _consume_signature_gate_override,
    _dedupe_payment_probe_strings,
    _element_info_is_submit_control,
    _execute_playwright_line,
    _fill_text_locator_verified,
    _inject_refs_into_aria_snapshot,
    _is_js_payment_action,
    _is_js_signature_action,
    _is_text_value_verification_target,
    _merchant_submit_owner_block_message,
    _normalize_browser_run_script_js,
    _payment_block_message,
    _payment_field_block_from_locator,
    _payment_gate_block_from_locator,
    _payment_probe_strings_from_element_info,
    _select_dropdown_value,
)
from services.browser.api_log_redaction import format_api_log_entry
from services.playwright_cdp_client import (
    PlaywrightCDPClient,
    PlaywrightCDPCommandError,
    PlaywrightCDPCommandTimeoutError,
    PlaywrightCDPConnectionError,
)

logger = get_logger(__name__)
_CDP_OPERATION_ERRORS = (
    PlaywrightCDPCommandTimeoutError,
    PlaywrightCDPCommandError,
    PlaywrightCDPConnectionError,
    RuntimeError,
    TimeoutError,
    TypeError,
    ValueError,
)

_SAFE = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_CONFIRM = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

server = FastMCP("viola-browser-cdp")

_MAX_TEXT = 5000
_CDP_COMMAND_TIMEOUT_SECONDS = 8.0
_CDP_TIMEOUT_RECOVERY_BASE_SECONDS = 3.0
_CDP_TIMEOUT_RECOVERY_MAX_SECONDS = 15.0
_CDP_LAST_NO_EFFECT_TARGET = ""
_CDP_LAST_NO_EFFECT_COUNT = 0
_CDP_REF_SELECTOR_MAP: dict[str, str] = {}
_CDP_REF_LOCATOR_MAP: dict[str, dict[str, Any]] = {}
_CDP_REF_RE = re.compile(r"^@?e\d+$")
_CDP_SUBMIT_CLICK_WAIT_ATTEMPTS = 30
_CDP_SUBMIT_CLICK_WAIT_INTERVAL_SECONDS = 0.4
_CDP_SNAPSHOT_MAX_REFS = 60
_CDP_SNAPSHOT_MAX_TEXT_CHARS = 1800
_CDP_SNAPSHOT_MAX_SELECTOR_CHARS = 220
_CDP_SNAPSHOT_MAX_LLM_CHARS = 12_000
_CDP_SNAPSHOT_MAX_CONTEXT_CHARS = 360
_BOGUS_PSEUDO_RE = re.compile(
    r"(?ix)(?::contains\s*\(|:has-text\s*\(|:icontains\s*\(|"
    r"\[\s*innerText\s*[*^$~|]?=\s*|\[\s*textContent\s*[*^$~|]?=\s*|"
    r"\[\s*innerHTML\s*[*^$~|]?=\s*)"
)

_CDP_REF_USAGE_DOC = (
    " Snapshot refs such as @e2 are element refs, not CSS selectors. "
    "Use the browser_snapshot selector_for_ref mapping when a CSS selector is needed, for example "
    "selector=selector_for_ref['@e2']. Do not pass @e2 itself as selector to clients that lack "
    "refs_resolve_server_side; this CDP server can resolve compact @eN refs from the latest snapshot."
)

_CDP_FILL_FORM_REF_DOC = (
    " browser_fill_form can accept @eN refs from the latest browser_snapshot or browser_get_form_fields "
    "because it resolves those refs through the stored selector map."
)

_CDP_SELECTOR_SYNTAX_DOC = (
    " Selector arguments accept standard browser CSS selectors such as tags, #id, .class, "
    "[attr=value], combinators, and standard pseudo-classes like :not(), :nth-child(), "
    ":checked, and browser-supported CSS :has(). They do not accept jQuery/Sizzle text selectors "
    "such as :contains(), :icontains(), or :has(:contains(...)); Playwright-only :has-text() "
    "is also not valid in CSS selector fields. For visible text matching, use browser_snapshot "
    "selector_for_ref, browser_get_text then inspect the returned text, or browser_evaluate with "
    "DOM text matching/XPath."
)


def _clear_cdp_refs() -> None:
    _CDP_REF_SELECTOR_MAP.clear()
    _CDP_REF_LOCATOR_MAP.clear()


def _is_cdp_ref(value: str) -> bool:
    return bool(_CDP_REF_RE.fullmatch(value.strip()))


class FormField(BaseModel):
    """A single form field to fill."""

    ref: str = Field(
        description=(
            "Element ref like e3/@e3 from browser_snapshot/browser_get_form_fields, "
            "or a standard CSS selector. For browser_interact use snapshot selector_for_ref instead."
        )
    )
    value: str = Field(description="The text to type, or true/false for checkboxes")
    select: bool | None = Field(default=None, description="Set true for dropdown/combobox fields")


def _store_cdp_refs(items: list[Any]) -> None:
    """Remember snapshot refs so later CDP tools can resolve @eN to selectors."""
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        selector = str(item.get("selector") or "").strip()
        if not ref or not selector:
            continue
        normalized = ref[1:] if ref.startswith("@") else ref
        _CDP_REF_SELECTOR_MAP[normalized] = selector
        _CDP_REF_SELECTOR_MAP["@" + normalized] = selector


def _resolve_cdp_selector(ref_or_selector: str) -> str | None:
    value = ref_or_selector.strip()
    if not value:
        return None
    if value in _CDP_REF_SELECTOR_MAP:
        return _CDP_REF_SELECTOR_MAP[value]
    normalized = value[1:] if value.startswith("@") else value
    if normalized in _CDP_REF_SELECTOR_MAP:
        return _CDP_REF_SELECTOR_MAP[normalized]
    if _is_css_selector(value):
        return value
    return None


class _CDPRefManager:
    """Small ref registry for Playwright ARIA snapshot refs."""

    def clear_ref_map(self) -> None:
        _clear_cdp_refs()

    def restrict_ref_map(self, visible_refs: set[str]) -> None:
        stale = [key for key in _CDP_REF_LOCATOR_MAP if key not in visible_refs and key.lstrip("@") not in visible_refs]
        for key in stale:
            _CDP_REF_LOCATOR_MAP.pop(key, None)
            _CDP_REF_SELECTOR_MAP.pop(key, None)

    def set_ref(
        self,
        ref_id: str,
        role: str,
        name: str,
        frame: Any = None,
        role_index: int = 0,
        name_index: int = 0,
    ) -> None:
        normalized = ref_id[1:] if ref_id.startswith("@") else ref_id
        entry = {
            "role": role,
            "name": name,
            "frame": frame,
            "role_index": role_index,
            "name_index": name_index,
        }
        _CDP_REF_LOCATOR_MAP[normalized] = entry
        _CDP_REF_LOCATOR_MAP["@" + normalized] = entry
        # Compatibility with existing CDP response shape. These refs now resolve
        # server-side through Playwright locators rather than CSS selector strings.
        _CDP_REF_SELECTOR_MAP[normalized] = "@" + normalized
        _CDP_REF_SELECTOR_MAP["@" + normalized] = "@" + normalized

    async def resolve_ref(self, ref_id: str) -> Any:
        normalized = ref_id[1:] if ref_id.startswith("@") else ref_id
        entry = _CDP_REF_LOCATOR_MAP.get(normalized) or _CDP_REF_LOCATOR_MAP.get("@" + normalized)
        if not entry:
            raise ValueError("Unknown or stale snapshot ref '@%s'" % normalized)
        page = manager.page
        target = entry.get("frame") or page
        role = str(entry.get("role") or "")
        name = str(entry.get("name") or "")
        if name:
            locator = target.get_by_role(role, name=name, exact=True)
            name_index = int(entry.get("name_index") or 0)
            if name_index > 0:
                locator = locator.nth(name_index)
            return locator
        return target.get_by_role(role).nth(int(entry.get("role_index") or 0))


_CDP_REF_MANAGER = _CDPRefManager()


async def _resolve_cdp_ref_locator(ref_or_selector: str) -> Any | None:
    value = ref_or_selector.strip()
    if not _is_cdp_ref(value):
        return None
    try:
        return await _CDP_REF_MANAGER.resolve_ref(value)
    except ValueError:
        if _resolve_cdp_selector(value):
            return None
        raise


def _compact_cdp_selector_for_llm(ref: str, selector: str) -> str:
    if len(selector) <= _CDP_SNAPSHOT_MAX_SELECTOR_CHARS:
        return selector
    return ref


def _cdp_snapshot_line(item: dict[str, Any]) -> str:
    role = str(item.get("role") or "")
    ref = str(item.get("ref") or "")
    item_type = str(item.get("type") or "")
    text = str(item.get("text") or "")
    selector = str(item.get("selector") or "")
    selector_display = _compact_cdp_selector_for_llm(ref, selector) if ref and selector else selector
    type_text = " type=%s" % json.dumps(item_type) if item_type else ""
    metadata: list[str] = []
    state = item.get("state")
    if isinstance(state, dict):
        compact_state = {str(key): str(value) for key, value in state.items() if value is not None and str(value) != ""}
        if compact_state:
            metadata.append("state=%s" % json.dumps(compact_state, sort_keys=True))
    duplicate = item.get("duplicate_text")
    if isinstance(duplicate, dict):
        metadata.append("duplicate_text=%s" % json.dumps(duplicate, sort_keys=True))
    context = str(item.get("context") or "").strip()
    if context:
        metadata.append("context=%s" % json.dumps(_truncate(context, _CDP_SNAPSHOT_MAX_CONTEXT_CHARS)))
    metadata_text = (" " + " ".join(metadata)) if metadata else ""
    return "%s %s%s %s%s selector=%s" % (
        role,
        ref,
        type_text,
        json.dumps(text),
        metadata_text,
        json.dumps(selector_display),
    )


_ARIA_REF_LINE_RE = re.compile(r"^\s*-\s+(\w+)\s+@?(e\d+)(.*)$")


def _cdp_important_controls_from_snapshot(snapshot_text: str) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for line in snapshot_text.splitlines():
        match = _ARIA_REF_LINE_RE.match(line)
        if not match:
            continue
        role = match.group(1)
        ref = "@%s" % match.group(2)
        rest = match.group(3)
        name_match = re.search(r'"([^"]*)"', rest)
        name = name_match.group(1) if name_match else ""
        controls.append(
            {
                "ref": ref,
                "role": role,
                "text": name,
                "state": {},
                "context": "",
                "duplicate_text": {},
            }
        )
        if len(controls) >= 12:
            break
    return controls


def _bogus_selector_error(selector: str) -> str | None:
    if selector and _BOGUS_PSEUDO_RE.search(selector):
        return _json(
            {
                "ok": False,
                "error": (
                    "Selector '%s' uses jQuery-style pseudo-selectors (:contains, [innerText=...], etc.) "
                    "that are not valid CSS."
                )
                % selector[:120],
                "invalid_selector_syntax": True,
                "selector": selector[:120],
                "invalid_patterns": [
                    ":contains",
                    "[innerText=...]",
                    "[textContent=...]",
                    "[innerHTML=...]",
                ],
            }
        )
    return None


def _reset_cdp_no_effect_state() -> None:
    global _CDP_LAST_NO_EFFECT_COUNT, _CDP_LAST_NO_EFFECT_TARGET

    _CDP_LAST_NO_EFFECT_TARGET = ""
    _CDP_LAST_NO_EFFECT_COUNT = 0


async def _browser_payment_observation_refusal(tool_name: str) -> str | None:
    from services.payments.browser_payment_guard import (
        browser_payment_block_payload,
        browser_payment_sensitive_active,
    )

    if not browser_payment_sensitive_active():
        return None
    return _json(browser_payment_block_payload(tool_name))


def _cdp_probe_strings_from_info(info: dict[str, Any] | None, *fallback_values: str) -> list[str]:
    values = [value for value in fallback_values if isinstance(value, str)]
    values.extend(_payment_probe_strings_from_element_info(info))
    if isinstance(info, dict):
        values.extend(
            str(info.get(key) or "")
            for key in (
                "formText",
                "pageTitle",
            )
        )
    return _dedupe_payment_probe_strings(values)


def _cdp_page_has_signature_context(info: dict[str, Any] | None) -> bool:
    if not isinstance(info, dict):
        return False
    lower_page = str(info.get("pageText") or "").lower()
    lower_url = str(info.get("url") or "").lower()
    return bool(_SIGNATURE_URL_RE.search(lower_url)) or any(marker in lower_page for marker in _SIGNATURE_PAGE_MARKERS)


async def _cdp_signature_gate_block_from_info(
    info: dict[str, Any] | None,
    *fallback_values: str,
) -> str | None:
    if not _cdp_page_has_signature_context(info):
        return None
    input_type = str((info or {}).get("inputType") or "").lower()
    role = str((info or {}).get("role") or "").lower()
    is_signature_control = input_type in {"checkbox", "radio"} or role in {
        "checkbox",
        "radio",
        "switch",
    }
    action_signal = ""
    for candidate in _cdp_probe_strings_from_info(info, *fallback_values):
        lower_candidate = candidate.lower()
        if any(marker in lower_candidate for marker in _SIGNATURE_ACTION_MARKERS):
            action_signal = candidate
            break
    if not is_signature_control and not action_signal:
        return None
    if _consume_signature_gate_override():
        logger.info(
            "Signature gate override allowed CDP action through (signal=%r)",
            action_signal[:80],
        )
        return None
    if is_signature_control:
        return (
            "LEGAL SIGNATURE DETECTED: Do NOT check signature or certification controls yet. "
            'Call signature(action="request_review", ...) with authority, document, signer, and certification details first.'
        )
    return (
        "SIGNATURE GATE BLOCKED: This action would apply a legal signature or continue past a "
        "certification page. "
        'Call signature(action="request_review", ...) with authority, document, signer, and certification details first.'
    )


async def _cdp_payment_gate_block_from_info(
    info: dict[str, Any] | None,
    *fallback_values: str,
) -> str | None:
    for candidate in _cdp_probe_strings_from_info(info, *fallback_values):
        blocked = _payment_block_message(candidate)
        if not blocked:
            continue
        if _consume_payment_gate_override():
            if _ORDER_SUBMIT_RE.search(candidate.strip().lower()):
                merchant_submit_block = await _merchant_submit_owner_block_message(candidate)
                if merchant_submit_block:
                    return merchant_submit_block
            logger.info(
                "Payment gate override allowed CDP action through (signal=%r)",
                candidate[:60],
            )
            return None
        return blocked
    return None


async def _cdp_action_gate_block_from_info(
    info: dict[str, Any] | None,
    *fallback_values: str,
) -> str | None:
    if blocked := await _cdp_signature_gate_block_from_info(info, *fallback_values):
        return blocked
    return await _cdp_payment_gate_block_from_info(info, *fallback_values)


def _cdp_payment_field_block_from_info(info: dict[str, Any] | None, *fallback_values: str) -> str | None:
    candidates = [value for value in fallback_values if isinstance(value, str)]
    candidates.extend(_payment_probe_strings_from_element_info(info))
    for candidate in _dedupe_payment_probe_strings(candidates):
        lower = candidate.lower()
        if any(keyword in lower for keyword in _PAYMENT_FIELD_KEYWORDS):
            return (
                "PAYMENT SAFETY VIOLATION: Do NOT fill payment fields directly. "
                'Call payment(action="request_review", ...) with merchant, total, and order summary first.'
            )
    return None


async def _cdp_fill_gate_block_from_info(
    info: dict[str, Any] | None,
    *fallback_values: str,
) -> str | None:
    if blocked := _cdp_payment_field_block_from_info(info, *fallback_values):
        return blocked
    return await _cdp_action_gate_block_from_info(info, *fallback_values)


async def _cdp_element_info_at_point(x: float, y: float) -> dict[str, Any]:
    try:
        result = await manager.evaluate_js("""((x, y) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return {};
                const form = el.closest ? el.closest('form') : null;
                return {
                    text: (el.innerText || el.textContent || '').substring(0, 200),
                    value: ('value' in el && el.value != null) ? String(el.value).substring(0, 200) : '',
                    ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 200),
                    nameAttr: (el.getAttribute('name') || '').substring(0, 200),
                    title: (el.getAttribute('title') || '').substring(0, 200),
                    role: (el.getAttribute('role') || '').toLowerCase(),
                    tag: (el.tagName || '').toLowerCase(),
                    inputType: (el.type || '').toLowerCase(),
                    name: (
                        el.getAttribute('aria-label')
                        || el.getAttribute('name')
                        || el.getAttribute('title')
                        || el.innerText
                        || el.textContent
                        || ''
                    ).substring(0, 200),
                    formText: form ? (form.innerText || form.textContent || '').substring(0, 500) : '',
                    pageTitle: document.title || '',
                    url: location.href,
                    pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
                };
            })(%s, %s)""" % (json.dumps(x), json.dumps(y)))
    except _CDP_OPERATION_ERRORS:
        return {}
    return result if isinstance(result, dict) else {}


async def _cdp_element_info_for_selector(selector: str) -> dict[str, Any]:
    try:
        result = await manager.evaluate_js("""((sel) => {
                const el = document.querySelector(sel);
                if (!el) return {};
                const form = el.closest ? el.closest('form') : null;
                return {
                    text: (el.innerText || el.textContent || '').substring(0, 200),
                    value: ('value' in el && el.value != null) ? String(el.value).substring(0, 200) : '',
                    ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 200),
                    nameAttr: (el.getAttribute('name') || '').substring(0, 200),
                    title: (el.getAttribute('title') || '').substring(0, 200),
                    role: (el.getAttribute('role') || '').toLowerCase(),
                    tag: (el.tagName || '').toLowerCase(),
                    inputType: (el.type || '').toLowerCase(),
                    name: (
                        el.getAttribute('aria-label')
                        || el.getAttribute('name')
                        || el.getAttribute('title')
                        || el.innerText
                        || el.textContent
                        || ''
                    ).substring(0, 200),
                    formText: form ? (form.innerText || form.textContent || '').substring(0, 500) : '',
                    pageTitle: document.title || '',
                    url: location.href,
                    pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
                };
            })(%s)""" % json.dumps(selector))
    except _CDP_OPERATION_ERRORS:
        return {}
    return result if isinstance(result, dict) else {}


async def _cdp_active_element_info() -> dict[str, Any]:
    try:
        result = await manager.evaluate_js("""(() => {
                const el = document.activeElement;
                if (!el) return {};
                const form = el.closest ? el.closest('form') : null;
                return {
                    text: (el.innerText || el.textContent || '').substring(0, 200),
                    value: ('value' in el && el.value != null) ? String(el.value).substring(0, 200) : '',
                    ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 200),
                    nameAttr: (el.getAttribute('name') || '').substring(0, 200),
                    title: (el.getAttribute('title') || '').substring(0, 200),
                    role: (el.getAttribute('role') || '').toLowerCase(),
                    tag: (el.tagName || '').toLowerCase(),
                    inputType: (el.type || '').toLowerCase(),
                    name: (
                        el.getAttribute('aria-label')
                        || el.getAttribute('name')
                        || el.getAttribute('title')
                        || el.innerText
                        || el.textContent
                        || ''
                    ).substring(0, 200),
                    formText: form ? (form.innerText || form.textContent || '').substring(0, 500) : '',
                    pageTitle: document.title || '',
                    url: location.href,
                    pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
                };
            })()""")
    except _CDP_OPERATION_ERRORS:
        return {}
    return result if isinstance(result, dict) else {}


async def _cdp_current_page_info() -> dict[str, Any]:
    try:
        result = await manager.evaluate_js("""(() => ({
                url: location.href,
                pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
            }))()""")
    except _CDP_OPERATION_ERRORS:
        return {}
    return result if isinstance(result, dict) else {}


async def _cdp_js_action_gate_block(script: str) -> str | None:
    if blocked := await _is_js_payment_action(script):
        return blocked
    return _is_js_signature_action(script)


async def _cdp_signature_gate_block_from_current_page_action(script: str) -> str | None:
    stripped = (script or "").strip()
    if not stripped or not _JS_SIGNATURE_ACTION_RE.search(stripped):
        return None
    info = await _cdp_current_page_info()
    if not _cdp_page_has_signature_context(info):
        return None
    if _consume_signature_gate_override():
        logger.info(
            "Signature gate override allowed CDP page action through (script=%r)",
            stripped[:80],
        )
        return None
    return (
        "SIGNATURE GATE BLOCKED: This script attempts to apply a legal signature or continue "
        'past a certification page. Call signature(action="request_review", ...) with authority, '
        "document, signer, and certification details first."
    )


async def _cdp_key_action_gate_block(key_lower: str) -> str | None:
    if key_lower not in {"enter", "return", "space"}:
        return None
    info = await _cdp_active_element_info()
    return await _cdp_action_gate_block_from_info(info, key_lower)


def _record_cdp_no_effect(target: str) -> int:
    global _CDP_LAST_NO_EFFECT_COUNT, _CDP_LAST_NO_EFFECT_TARGET

    normalized = " ".join(target.strip().lower().split())
    if normalized and normalized == _CDP_LAST_NO_EFFECT_TARGET:
        _CDP_LAST_NO_EFFECT_COUNT += 1
    else:
        _CDP_LAST_NO_EFFECT_TARGET = normalized
        _CDP_LAST_NO_EFFECT_COUNT = 1
    return _CDP_LAST_NO_EFFECT_COUNT


async def _cdp_page_fingerprint() -> str:
    """Return a compact page-state fingerprint for no-effect click detection."""
    try:
        url = await manager.get_url()
        title = await manager.get_title()
        dom_state = await manager.evaluate_js("""(() => {
                const fields = Array.from(document.querySelectorAll('input, textarea, select')).map((el) => ({
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                    type: el.getAttribute('type') || el.tagName,
                    value: el.value || '',
                    checked: !!el.checked,
                    selectedIndex: typeof el.selectedIndex === 'number' ? el.selectedIndex : null,
                }));
                return {body: document.body ? document.body.innerText : '', fields};
            })()""")
        return " ".join(json.dumps({"url": url, "title": title, "dom": dom_state}, default=str).split())
    except Exception:
        logger.debug("CDP page fingerprint failed")
        return ""


def _cdp_info_is_submit_control(info: dict[str, Any] | None) -> bool:
    return _element_info_is_submit_control(info)


def _cdp_info_is_focusable_form_control(info: dict[str, Any] | None) -> bool:
    if not isinstance(info, dict):
        return False
    tag = str(info.get("tag") or "").strip().lower()
    input_type = str(info.get("inputType") or info.get("type") or "").strip().lower()
    if tag in {"select", "textarea"}:
        return True
    if tag != "input":
        return False
    return input_type not in {"button", "hidden", "image", "reset", "submit"}


async def _cdp_begin_click_effect_watch() -> int:
    try:
        value = await manager.evaluate_js("""(() => {
                const key = '__violaClickEffectWatch';
                const previous = window[key];
                if (previous && previous.observer && typeof previous.observer.disconnect === 'function') {
                    previous.observer.disconnect();
                }
                const root = document.documentElement || document.body;
                if (!root || typeof MutationObserver !== 'function') {
                    window[key] = {count: 0, observer: null};
                    return 0;
                }
                const state = {count: 0, observer: null};
                state.observer = new MutationObserver((mutations) => {
                    state.count += mutations.length;
                });
                state.observer.observe(root, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true
                });
                window[key] = state;
                return state.count;
            })()""")
    except _CDP_OPERATION_ERRORS:
        logger.debug("CDP click-effect mutation watch setup failed")
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _cdp_click_mutation_count(*, disconnect: bool = False) -> int:
    try:
        value = await manager.evaluate_js("""((disconnect) => {
                const key = '__violaClickEffectWatch';
                const state = window[key];
                const count = state && typeof state.count === 'number' ? state.count : 0;
                if (disconnect && state && state.observer && typeof state.observer.disconnect === 'function') {
                    state.observer.disconnect();
                    delete window[key];
                }
                return count;
            })(%s)""" % json.dumps(bool(disconnect)))
    except _CDP_OPERATION_ERRORS:
        logger.debug("CDP click-effect mutation count failed")
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _cdp_click_focused_target(selector: str | None, x: float, y: float) -> bool:
    try:
        result = await manager.evaluate_js("""((sel, x, y) => {
                let el = null;
                if (sel) {
                    try { el = document.querySelector(sel); } catch (err) { el = null; }
                }
                if (!el) el = document.elementFromPoint(x, y);
                const active = document.activeElement;
                if (!el || !active) return false;
                return el === active || el.contains(active) || active.contains(el);
            })(%s, %s, %s)""" % (json.dumps(selector), json.dumps(x), json.dumps(y)))
    except _CDP_OPERATION_ERRORS:
        logger.debug("CDP focused-target verification failed")
        return False
    return bool(result)


async def _cdp_form_field_count() -> int:
    try:
        count = await manager.evaluate_js("document.querySelectorAll('input, textarea, select').length")
    except _CDP_OPERATION_ERRORS:
        logger.debug("CDP form field count failed")
        return 0
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def _cdp_wait_for_click_effect(
    url_before: str,
    fingerprint_before: str,
    field_count_before: int,
    mutation_count_before: int | None = None,
) -> bool:
    for _ in range(_CDP_SUBMIT_CLICK_WAIT_ATTEMPTS):
        try:
            if await manager.get_url() != url_before:
                return True
        except _CDP_OPERATION_ERRORS:
            logger.debug("CDP submit-click URL check failed")
        if mutation_count_before is not None:
            try:
                # DOM churn alone is not semantic progress. Dynamic pages can
                # mutate analytics, timers, or hidden containers while the
                # visible checkout state remains unchanged.
                await _cdp_click_mutation_count()
            except _CDP_OPERATION_ERRORS:
                logger.debug("CDP submit-click mutation check failed")
        try:
            current = await _cdp_page_fingerprint()
            if fingerprint_before and current and current != fingerprint_before:
                return True
        except _CDP_OPERATION_ERRORS:
            logger.debug("CDP submit-click fingerprint check failed")
        try:
            if await _cdp_form_field_count() != field_count_before:
                return True
        except _CDP_OPERATION_ERRORS:
            logger.debug("CDP submit-click field count check failed")
        await asyncio.sleep(_CDP_SUBMIT_CLICK_WAIT_INTERVAL_SECONDS)
    return False


async def _cdp_dom_click_target(selector: str | None, x: float, y: float) -> dict[str, Any]:
    result = await manager.evaluate_js("""((sel, x, y) => {
            let el = null;
            if (sel) {
                try { el = document.querySelector(sel); } catch (err) { el = null; }
            }
            if (!el) el = document.elementFromPoint(x, y);
            if (!el) return {ok: false, error: 'No element found for DOM click fallback'};
            el.scrollIntoView({block: 'center', inline: 'center'});
            if (typeof el.click !== 'function') return {ok: false, error: 'Element is not clickable'};
            el.click();
            return {
                ok: true,
                tag: (el.tagName || '').toLowerCase(),
                type: String(el.type || '').toLowerCase(),
                text: (el.innerText || el.value || el.textContent || '').trim().substring(0, 80)
            };
        })(%s, %s, %s)""" % (json.dumps(selector), json.dumps(x), json.dumps(y)))
    return result if isinstance(result, dict) else {"ok": bool(result)}


async def _cdp_verify_text_value(selector: str, expected: str) -> tuple[bool, str]:
    try:
        current = await manager.evaluate_js("""((sel) => {
                const el = document.querySelector(sel);
                return el && 'value' in el && el.value != null ? String(el.value) : '';
            })(%s)""" % json.dumps(selector))
    except _CDP_OPERATION_ERRORS:
        logger.debug("CDP text value verification failed")
        return False, ""
    return str(current) == str(expected), str(current)


# ---------------------------------------------------------------------------
# Key name → CDP key descriptor mapping
# ---------------------------------------------------------------------------

_KEY_MAP: dict[str, dict[str, Any]] = {
    "enter": {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "keyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "keyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "keyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "keyCode": 46},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "keyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "keyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "keyCode": 39},
    "home": {"key": "Home", "code": "Home", "keyCode": 36},
    "end": {"key": "End", "code": "End", "keyCode": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "keyCode": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "keyCode": 34},
    "space": {"key": " ", "code": "Space", "keyCode": 32, "text": " "},
}


# ---------------------------------------------------------------------------
# CDP Browser Manager
# ---------------------------------------------------------------------------


class CDPBrowserManager:
    """Manages a Playwright CDP connection and optional AgentPerception instance.

    Lazy-connects on first tool call.  Holds an optional ``webview``
    reference for Qt-level screenshots (set via ``set_webview``).
    """

    def __init__(self) -> None:
        self._cdp: PlaywrightCDPClient | None = None
        self._perception: Any | None = None  # AgentPerception
        self._webview: Any | None = None
        self._connect_lock = asyncio.Lock()
        self._timeout_circuit_until: float = 0.0
        self._consecutive_timeouts: int = 0
        self._last_timeout_method: str = ""

    def _raise_if_timeout_recovering(self) -> None:
        remaining = self._timeout_circuit_until - time.monotonic()
        if remaining <= 0:
            return
        method = self._last_timeout_method or "unknown CDP command"
        raise RuntimeError(
            "CDP browser session is recovering after CDP command timeout in %s; recovery_seconds=%.1f."
            % (method, remaining)
        )

    async def _handle_cdp_timeout(self, exc: PlaywrightCDPCommandError) -> None:
        method = getattr(exc, "method", "") or "unknown CDP command"
        self._consecutive_timeouts += 1
        self._last_timeout_method = method
        delay = min(
            _CDP_TIMEOUT_RECOVERY_MAX_SECONDS,
            _CDP_TIMEOUT_RECOVERY_BASE_SECONDS * (2 ** max(0, self._consecutive_timeouts - 1)),
        )
        self._timeout_circuit_until = time.monotonic() + delay
        logger.warning(
            "CDP browser manager invalidating session after command timeout in %s; recovery %.1fs",
            method,
            delay,
        )
        await self.close(reset_timeout_state=False)

    def _reset_timeout_circuit(self) -> None:
        self._timeout_circuit_until = 0.0
        self._consecutive_timeouts = 0
        self._last_timeout_method = ""

    async def ensure_connected(self) -> None:
        """Connect Playwright to the desktop CDP endpoint if needed."""
        self._raise_if_timeout_recovering()
        if self._cdp is not None and self._cdp.connected:
            return

        async with self._connect_lock:
            if self._cdp is not None and self._cdp.connected:
                return
            try:
                from config.settings import settings

                port = int(getattr(settings, "cdp_port", 0) or 0)
                if port <= 0:
                    raise RuntimeError(
                        "Viola's own built-in browser is switched off in this installation, so "
                        "it never started and no website was contacted. Nothing is known about "
                        "whether the site is reachable. Turning it on is a local configuration "
                        "change (the VIOLA_CDP_PORT setting); retrying will not help until then."
                    )
                self._cdp = PlaywrightCDPClient(port=port, command_timeout=_CDP_COMMAND_TIMEOUT_SECONDS)
                await self._cdp.connect()

                # Create perception layer
                from services.agent_perception import AgentPerception

                self._perception = AgentPerception(
                    cdp_client=self._cdp,
                    webview=self._webview,
                )
                logger.info("CDP browser manager connected on port %d via Playwright", port)
            except Exception:
                logger.exception("CDP browser manager: connection failed")
                self._cdp = None
                self._perception = None
                raise

    async def _run_cdp_operation(self, method: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        await self.ensure_connected()
        try:
            result = await operation()
        except PlaywrightCDPCommandTimeoutError as exc:
            await self._handle_cdp_timeout(exc)
            raise
        except PlaywrightCDPCommandError as exc:
            if getattr(exc, "is_timeout", False):
                await self._handle_cdp_timeout(exc)
            raise
        self._reset_timeout_circuit()
        return result

    @property
    def cdp(self) -> Any:
        """Return the Playwright CDP client, or raise if not connected."""
        if self._cdp is None or not self._cdp.connected:
            raise RuntimeError("CDP client is not connected")
        return self._cdp

    @property
    def page(self) -> Any:
        """Return the current Playwright Page for the Qt overlay."""
        return self.cdp.page

    @property
    def perception(self) -> Any | None:
        """Return the AgentPerception, or None."""
        return self._perception

    @property
    def is_connected(self) -> bool:
        return self._cdp is not None and self._cdp.connected

    def set_webview(self, webview: Any) -> None:
        """Set the Qt QWebEngineView reference for OS-level screenshots."""
        self._webview = webview
        if self._perception is not None:
            self._perception.set_webview(webview)

    async def evaluate_js(self, expression: str) -> Any:
        """Evaluate JS via CDP. Ensures connection first."""
        return await self._run_cdp_operation(
            "Page.evaluate",
            lambda: self.cdp.evaluate_js(expression),
        )

    async def get_url(self) -> str:
        return await self._run_cdp_operation("Page.url", lambda: self.cdp.get_url())

    async def get_title(self) -> str:
        return await self._run_cdp_operation("Page.title", lambda: self.cdp.get_title())

    async def stop_loading(self) -> None:
        await self._run_cdp_operation("Page.stopLoading", lambda: self.cdp.stop_loading())

    async def navigate(self, url: str) -> dict[str, Any]:
        return await self._run_cdp_operation("Page.goto", lambda: self.cdp.navigate(url))

    async def click(self, x: float, y: float) -> None:
        await self._run_cdp_operation("Mouse.click", lambda: self.cdp.click(x, y))

    async def type_text(self, text: str) -> None:
        await self._run_cdp_operation("Keyboard.insert_text", lambda: self.cdp.type_text(text))

    async def press_key(self, key: str) -> None:
        await self._run_cdp_operation("Keyboard.press", lambda: self.cdp.press_key(key))

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("Raw CDP sends are not available; use Playwright page APIs")

    async def aria_snapshot(self) -> str:
        return await self._run_cdp_operation("Locator.aria_snapshot", lambda: self.cdp.aria_snapshot())

    async def screenshot(self) -> bytes:
        return await self._run_cdp_operation("Page.screenshot", lambda: self.cdp.screenshot())

    async def get_api_log(
        self,
        domain_filter: str | None = None,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_connected()
        if not user_id:
            return []
        # Bind the active capture owner so events received between calls
        # land in the right per-user bucket.
        self.cdp.bind_user(user_id)
        return self.cdp.get_api_log(domain_filter, user_id=user_id)

    async def close(self, *, reset_timeout_state: bool = True) -> None:
        """Disconnect and clean up."""
        if self._cdp is not None:
            try:
                await self._cdp.close()
            except Exception:
                logger.debug("CDP close error (non-critical)")
            self._cdp = None
            self._perception = None
        if reset_timeout_state:
            self._reset_timeout_circuit()


manager = CDPBrowserManager()


def set_webview(view: Any) -> None:
    """Module-level setter for Qt integration.

    Called from ``viola_qt.py`` after the browser webview is created.
    """
    _clear_cdp_refs()
    manager.set_webview(view)


async def dispatch_input_event(event_data: dict[str, Any]) -> dict[str, Any]:
    """Forward a browser stream input event to the visible CDP page.

    This is used by spoke/cloud renderers that see JPEG frames instead of the
    native Qt webview. Hub users interact with the QWebEngineView directly.
    """
    event_type = event_data.get("type")
    if event_type != "mouse_click":
        return {"ok": False, "error": "Unsupported browser input event"}

    await manager.ensure_connected()
    viewport = await manager.evaluate_js("({width: window.innerWidth || 0, height: window.innerHeight || 0})")
    viewport_width = 0.0
    viewport_height = 0.0
    if isinstance(viewport, dict):
        viewport_width = float(viewport.get("width") or 0)
        viewport_height = float(viewport.get("height") or 0)
    if viewport_width <= 0:
        viewport_width = float(event_data.get("width") or 0)
    if viewport_height <= 0:
        viewport_height = float(event_data.get("height") or 0)
    if viewport_width <= 0 or viewport_height <= 0:
        return {"ok": False, "error": "Browser viewport size is unavailable"}

    x_ratio = max(0.0, min(1.0, float(event_data.get("x_ratio") or 0)))
    y_ratio = max(0.0, min(1.0, float(event_data.get("y_ratio") or 0)))
    x = x_ratio * viewport_width
    y = y_ratio * viewport_height
    await manager.click(x, y)
    return {"ok": True, "x": x, "y": y}


async def cancel_active_operation() -> None:
    """Best-effort interruption hook used when the user takes over mid-tool."""
    if not manager.is_connected:
        return
    try:
        await manager.stop_loading()
    except Exception:
        logger.debug("CDP active operation cancellation failed", exc_info=True)


# ===========================================================================
# NAVIGATION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "LAST RESORT for content reading: open a specific web page in the visible CDP browser and return title, final URL, and page metadata. "
        "Use when the page needs JavaScript, login, forms, account/commerce flow, screenshots, or other live interaction, or when web_read cannot extract enough content. "
        "For plain public articles, news, blogs, docs, and fetchable pages found by web_search, web_read is often the lighter reading tool. "
        "When the correct URL is uncertain, use web_search first and inspect the result trust signals before navigating. "
        "Input is a URL string; bare domains are normalized to https://."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_navigate(url: str) -> str:
    """Navigate to a URL. Returns the page title, final URL, and brief description.

    Args:
        url: The URL to navigate to (https:// prefix added if missing).
    """
    try:
        normalized_url, normalize_error = _normalize_navigation_url(url)
        if normalize_error or not normalized_url:
            payload = {
                "ok": False,
                "success": False,
                "error": normalize_error or "URL must not be empty",
            }
            payload.update(_navigation_fact_fields(normalized_url or url, error_text=payload["error"]))
            return _json(payload)
        url = normalized_url

        if url != "about:blank":
            url_error = _validate_url(url)
            if url_error:
                payload = {"ok": False, "success": False, "error": url_error}
                payload.update(_navigation_fact_fields(url, error_text=url_error))
                return _json(payload)

        await manager.ensure_connected()
        navigate_result = await manager.navigate(url)
        _clear_cdp_refs()
        _reset_cdp_no_effect_state()
        cdp_result = navigate_result.get("result") if isinstance(navigate_result, dict) else {}
        error_text = ""
        if isinstance(cdp_result, dict):
            error_text = str(cdp_result.get("errorText") or "")
        if error_text:
            payload = {
                "ok": False,
                "success": False,
                "error": "Navigation failed: %s" % error_text,
            }
            payload.update(_navigation_fact_fields(url, error_text=error_text))
            return _json(payload)
        # Get page metadata via JS (same data as Playwright server)
        info = await manager.evaluate_js("""(() => {
                const meta = document.querySelector('meta[name="description"]');
                return {
                    title: document.title,
                    url: location.href,
                    description: meta ? meta.content : '',
                    body_text: (document.body ? document.body.innerText : '').slice(0, 4000),
                };
            })()""")
        # #579: the CDP navigate DOES carry the real HTTP status
        # (services/playwright_cdp_client.py returns {"result": {"status": ...}}
        # from page.goto). Plumb it into _http_error_page_payload so an HTTP >=400
        # page (e.g. the #278 Cloudflare 524 gateway-timeout page) sets
        # http_error_page on the desktop surface too, not just via title/url text.
        http_status = cdp_result.get("status") if isinstance(cdp_result, dict) else None
        http_status = http_status if isinstance(http_status, int) else None
        if isinstance(info, dict):
            info.update(_navigation_fact_fields(str(info.get("url") or url), http_status_code=http_status))
            # Structured signal for the agent loop — successful navigate
            # invalidates any @eN refs from prior snapshots.
            info["refs_invalidated"] = True
            info["ref_invalidation_reason"] = "navigate"
            if error_page := _http_error_page_payload(
                title=info.get("title"),
                url=info.get("url"),
                description=info.get("description"),
                status=http_status,
            ):
                info.update(error_page)
            # #579: surface the same bot_protection / parked-domain / dead-page
            # facts the Playwright server does, so the #575 loop halt can fire on
            # the desktop CDP surface. Scan the page body text (Cloudflare's
            # "Just a moment..." / "Checking your browser" challenge text lives
            # in the visible DOM, available on a connect_over_cdp page). body_text
            # is a detection-only sample and is not returned to the model.
            body_text = str(info.pop("body_text", "") or "")
            page_health = detect_page_health(info.get("title"), info.get("description"), body_text)
            if page_health:
                info["page_health"] = page_health
            return _json(info)
        title = await manager.get_title()
        final_url = await manager.get_url()
        result = {
            "title": title,
            "url": final_url,
            "refs_invalidated": True,
            "ref_invalidation_reason": "navigate",
        }
        result.update(_navigation_fact_fields(final_url, http_status_code=http_status))
        if error_page := _http_error_page_payload(title=title, url=final_url, status=http_status):
            result.update(error_page)
        return _json(result)
    except Exception as exc:
        error_text = str(exc)
        lowered = error_text.lower()
        if any(token in lowered for token in ("err_name_not_resolved", "getaddrinfo")):
            error_text = "Domain does not resolve via DNS"
        payload = {
            "ok": False,
            "success": False,
            "error": "Navigation failed: %s" % error_text,
        }
        payload.update(_navigation_fact_fields(url, error_text=error_text))
        return _json(payload)


@server.tool(
    description=(
        "Go back one page in browser history via the CDP connection. "
        "This is the CDP equivalent of browser_navigate_back from the Playwright server. "
        "INPUT: No parameters required. "
        "SUCCESS: Returns JSON with the title and URL of the page you land on after going back. "
        "Waits 1 second for the page transition to settle before reading the new URL. "
        "FAILURE: Has no visible effect if there is no prior history entry; returns the current page "
        "title and URL unchanged. No error is raised. Also fails if the CDP connection is lost."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_back() -> str:
    """Go back one page in the browser history."""
    try:
        await manager.ensure_connected()
        await manager.evaluate_js("history.back()")
        _clear_cdp_refs()
        _reset_cdp_no_effect_state()
        # Wait for navigation
        await asyncio.sleep(1.0)
        title = await manager.get_title()
        url = await manager.get_url()
        return _json(
            {
                "title": title,
                "url": url,
                "refs_invalidated": True,
                "ref_invalidation_reason": "back",
            }
        )
    except Exception as exc:
        return _json({"error": "Go back failed: %s" % exc})


@server.tool(
    description=(
        "Go forward one page in browser history via the CDP connection. "
        "This only works if you previously went back; it does not advance to an arbitrary page. "
        "INPUT: No parameters required. "
        "SUCCESS: Returns JSON with the title and URL of the page you land on after going forward. "
        "Waits 1 second for the page transition to settle before reading the new URL. "
        "FAILURE: Has no visible effect if there is no forward history entry (e.g., you never went back). "
        "Returns the current page title and URL unchanged. No error is raised."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_forward() -> str:
    """Go forward one page in the browser history."""
    try:
        await manager.ensure_connected()
        await manager.evaluate_js("history.forward()")
        _clear_cdp_refs()
        _reset_cdp_no_effect_state()
        await asyncio.sleep(1.0)
        title = await manager.get_title()
        url = await manager.get_url()
        return _json(
            {
                "title": title,
                "url": url,
                "refs_invalidated": True,
                "ref_invalidation_reason": "forward",
            }
        )
    except Exception as exc:
        return _json({"error": "Go forward failed: %s" % exc})


@server.tool(
    description=(
        "Reload the current visible browser page via Playwright. "
        "INPUT: No parameters required. "
        "SUCCESS: Returns JSON with the refreshed page title and URL. Waits up to 30 seconds for "
        "domcontentloaded; continues silently if the event does not fire (e.g., slow sites). "
        "FAILURE: Returns error if the CDP connection is lost. The 30-second timeout does not produce an "
        "error; it is a best-effort wait."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_refresh() -> str:
    """Reload the current page."""
    try:
        await manager.ensure_connected()
        try:
            await manager.page.reload(wait_until="domcontentloaded", timeout=30000)
        except _CDP_OPERATION_ERRORS:
            pass  # best-effort wait
        _clear_cdp_refs()
        _reset_cdp_no_effect_state()
        title = await manager.get_title()
        url = await manager.get_url()
        return _json(
            {
                "title": title,
                "url": url,
                "refs_invalidated": True,
                "ref_invalidation_reason": "refresh",
            }
        )
    except Exception as exc:
        return _json({"error": "Refresh failed: %s" % exc})


# ===========================================================================
# READING TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Extract raw innerText from the page or a specific element via CDP. "
        "Returns the full page body text or text scoped to a CSS selector. "
        "INPUT: selector (optional) -- CSS selector like '#content', '.price', 'main'. Empty = full page body.\n"
        "SUCCESS: Returns {url, selector, text} truncated to 5000 chars.\n"
        "FAILURE: ok=false if the selector is invalid or matches no elements; error if CDP disconnected.\n"
        "EXAMPLES:\n"
        "- browser_get_text() -> full page body text\n"
        "- browser_get_text(selector='#product-description') -> text of that specific element\n"
        "- browser_get_text(selector='.search-results') -> text within search results container"
        + _CDP_SELECTOR_SYNTAX_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_get_text(selector: str = "") -> str:
    """Get text content from the page or a specific element.

    Args:
        selector: CSS selector (empty = full page body text, truncated to 5000 chars).
    """
    if block := await _browser_payment_observation_refusal("browser_get_text"):
        return block
    try:
        target = selector.strip() if selector else "body"
        if bogus := _bogus_selector_error(target):
            return bogus
        data = await manager.evaluate_js("""((sel) => {
                let els;
                try {
                    els = Array.from(document.querySelectorAll(sel));
                } catch (err) {
                    return {ok: false, error: 'Invalid CSS selector: ' + (err && err.message ? err.message : String(err))};
                }
                if (els.length === 0) {
                    return {ok: false, error: 'Selector matched 0 elements: ' + sel, count: 0};
                }
                const el = els[0];
                return {
                    ok: true,
                    text: el ? (el.innerText || '') : '',
                    count: els.length,
                };
            })(%s)""" % json.dumps(target))
        url = await manager.get_url()
        if isinstance(data, dict) and data.get("ok") is False:
            payload = {
                "ok": False,
                "url": url,
                "selector": target,
                "error": data.get("error") or "Selector lookup failed",
                "match_count": data.get("count", 0),
            }
            payload.update(_navigation_fact_fields(url, error_text=str(payload["error"])))
            return _json(payload)
        if isinstance(data, dict):
            return _json(
                {
                    "url": url,
                    "selector": target,
                    "text": _truncate(str(data.get("text") or "")),
                    "match_count": data.get("count", 1),
                }
            )
        return _json({"url": url, "selector": target, "text": _truncate(str(data or ""))})
    except _CDP_OPERATION_ERRORS as exc:
        current_url = ""
        try:
            current_url = await manager.get_url()
        except _CDP_OPERATION_ERRORS:
            logger.debug("Could not read current CDP URL for browser_get_text error")
        payload = {
            "ok": False,
            "error": "Failed to get text for '%s': %s" % (selector, exc),
        }
        payload.update(_navigation_fact_fields(current_url, error_text=str(exc)))
        return _json(payload)


@server.tool(
    description=(
        "Get all anchor links on the page as {text, href} pairs with full URLs via CDP. "
        "Returns actual href URLs for anchor elements. Buttons are not anchors. "
        "INPUT: selector (optional) -- CSS selector to scope ('nav', '.results'). Empty = whole page.\n"
        "SUCCESS: Returns {url, links: [{index, text, href}, ...], count} up to 50 links.\n"
        "FAILURE: Falls back to document.body if selector not found. Empty array if no links.\n"
        "EXAMPLES:\n"
        "- browser_get_links() -> all links on the page\n"
        "- browser_get_links(selector='.search-results') -> links within search results only" + _CDP_SELECTOR_SYNTAX_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_get_links(selector: str = "") -> str:
    """Get all links on the page as a JSON array of {text, href}.

    Args:
        selector: Optional CSS selector to scope the search (empty = whole page).
    """
    if block := await _browser_payment_observation_refusal("browser_get_links"):
        return block
    try:
        scope = selector.strip() if selector else "body"
        links = await manager.evaluate_js("""((scope) => {
                const root = scope === 'body'
                    ? document.body
                    : document.querySelector(scope) || document.body;
                return Array.from(root.querySelectorAll('a[href]')).slice(0, 50).map((a, i) => ({
                    index: i,
                    text: (a.innerText || '').trim().substring(0, 120),
                    href: a.href,
                }));
            })(%s)""" % json.dumps(scope))
        url = await manager.get_url()
        if not isinstance(links, list):
            links = []
        return _json({"url": url, "links": links, "count": len(links)})
    except Exception as exc:
        return _json({"error": "Failed to get links: %s" % exc})


@server.tool(
    description=(
        "Discover all visible form fields and submit buttons on the current page via CDP JavaScript evaluation. "
        "The output gives standard CSS selectors suitable for browser_fill_form and "
        "browser_interact (action='select'). "
        "INPUT: No parameters required. Scans the entire page automatically. "
        "SUCCESS: Returns JSON with url, a fields array (each with index, name, type, id, value, placeholder, "
        "label, and a CSS selector), a count of fields, and a submit_buttons array (each with text and selector). "
        "Hidden inputs and reCAPTCHA fields are excluded. Selectors are generated as #id, [name=...], or "
        "input:nth-of-type(N) as fallback. Buttons include both type=submit buttons and action buttons "
        "matching common patterns (Submit, Continue, Next, Place Order, Add to Cart, Find Store). "
        "FAILURE: Returns empty fields and buttons arrays if no visible form elements exist — the page may "
        "not have a form, or forms may be rendered in iframes or shadow DOM."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_form_fields() -> str:
    """Get all visible form input fields and submit buttons.

    Returns JSON with fields array and buttons array. Hidden inputs and
    recaptcha fields are excluded to reduce noise.
    """
    if block := await _browser_payment_observation_refusal("browser_get_form_fields"):
        _clear_cdp_refs()
        return block
    _clear_cdp_refs()
    try:
        data = await manager.evaluate_js("""(() => {
                const inputs = Array.from(document.querySelectorAll(
                    'input, textarea, select'
                )).filter(el => {
                    if (el.type === 'hidden') return false;
                    if (el.name && el.name.includes('recaptcha')) return false;
                    if (el.id && el.id.includes('recaptcha')) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                const fields = inputs.map((el, i) => {
                    let label = '';
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) label = lbl.innerText.trim();
                    }
                    if (!label) {
                        const parent = el.closest('label, [class*=field], [class*=input]');
                        if (parent) {
                            const txt = parent.innerText?.trim();
                            if (txt && txt.length < 60) label = txt;
                        }
                    }
                    const sel = el.id ? '#' + CSS.escape(el.id)
                        : el.name ? '[name="' + el.name + '"]'
                        : 'input:nth-of-type(' + (i + 1) + ')';
                    return {
                        index: i,
                        name: el.name || '',
                        type: el.type || el.tagName.toLowerCase(),
                        id: el.id || '',
                        value: (el.value || '').slice(0, 50),
                        placeholder: el.placeholder || '',
                        label: label,
                        selector: sel,
                    };
                });
                const btns = Array.from(document.querySelectorAll(
                    'button[type=submit], input[type=submit], ' +
                    'button.submit, button[class*=submit], button[class*=continue], ' +
                    'button[data-testid*=submit], button[data-testid*=continue]'
                )).slice(0, 5).map(b => ({
                    text: (b.innerText || b.value || '').trim().slice(0, 60),
                    selector: b.id ? '#' + CSS.escape(b.id)
                        : b.className ? 'button.' + b.className.split(' ')[0]
                        : 'button[type=submit]',
                }));
                const actionBtns = Array.from(document.querySelectorAll('button'))
                    .filter(b => {
                        const t = (b.innerText || '').toLowerCase();
                        return /submit|continue|next|proceed|place order|add to|find store/i.test(t);
                    })
                    .slice(0, 5).map(b => ({
                        text: (b.innerText || '').trim().slice(0, 60),
                        selector: b.id ? '#' + CSS.escape(b.id)
                            : 'button',
                    }));
                const allBtns = [...btns, ...actionBtns];
                const seen = new Set();
                const uniqueBtns = allBtns.filter(b => {
                    if (seen.has(b.text)) return false;
                    seen.add(b.text);
                    return true;
                });
                return {
                    fields: fields.map((field, i) => ({...field, ref: '@e' + (i + 1)})),
                    buttons: uniqueBtns.map((button, i) => ({...button, ref: '@e' + (fields.length + i + 1)})),
                };
            })()""")
        url = await manager.get_url()
        if not isinstance(data, dict):
            data = {"fields": [], "buttons": []}
        _store_cdp_refs(list(data.get("fields") or []))
        _store_cdp_refs(list(data.get("buttons") or []))
        return _json(
            {
                "url": url,
                "fields": data.get("fields", []),
                "count": len(data.get("fields", [])),
                "submit_buttons": data.get("buttons", []),
            }
        )
    except Exception as exc:
        return _json({"error": "Failed to get form fields: %s" % exc})


@server.tool(
    description=(
        "Quick lightweight page overview: URL, title, description, buttons, inputs, links. "
        "Returns a compact metadata and visible-control summary without a full snapshot. "
        "INPUT: No parameters.\n"
        "SUCCESS: Returns {title, url, description, buttons: [...up to 15], inputs: [...up to 15], links: [...up to 10]}.\n"
        "FAILURE: Error if CDP connection lost or JS evaluation fails. Empty arrays if no interactive elements.\n"
        "EXAMPLES:\n"
        "- browser_get_page_info() -> {title: 'Amazon.com: laptop', url: '...', buttons: ['Add to Cart', 'Buy Now'], inputs: [{type: 'text', name: 'quantity'}], links: ['Home', 'Electronics']}"
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_page_info() -> str:
    """Get current URL, title, meta description, and main interactive elements."""
    if block := await _browser_payment_observation_refusal("browser_get_page_info"):
        return block
    try:
        info = await manager.evaluate_js("""(() => {
                const meta = document.querySelector('meta[name="description"]');
                const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'))
                    .slice(0, 15).map(b => {
                        const text = b.innerText?.trim() || b.value || '';
                        const type = b.type || '';
                        const cls = b.className?.toString().slice(0, 40) || '';
                        return text || (type ? '[type=' + type + ']' : '') || (cls ? '[class=' + cls + ']' : '');
                    }).filter(Boolean);
                const inputs = Array.from(document.querySelectorAll('input, textarea, select'))
                    .slice(0, 15).map(el => ({
                        type: el.type || el.tagName.toLowerCase(),
                        name: el.name || el.id || '',
                        placeholder: el.placeholder || '',
                        value: (el.value || '').slice(0, 30),
                    }));
                const links = Array.from(document.querySelectorAll('a[href]'))
                    .slice(0, 10).map(a => a.innerText.trim()).filter(Boolean);
                return {
                    title: document.title,
                    url: location.href,
                    description: meta ? meta.content : '',
                    buttons: buttons,
                    inputs: inputs,
                    links: links,
                };
            })()""")
        if isinstance(info, dict):
            return _json(info)
        return _json({"error": "Failed to parse page info"})
    except Exception as exc:
        return _json({"error": "Failed to get page info: %s" % exc})


@server.tool(
    description=(
        "Get the page's interactive snapshot with @eN refs and selector_for_ref entries for clicking, "
        "filling, and selecting elements." + _CDP_REF_USAGE_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_snapshot(
    wait_for_stable: bool = False,
    mode: str = "full",
) -> str:
    """Get fresh page state with @eN refs for CDP browser tools."""
    if block := await _browser_payment_observation_refusal("browser_snapshot"):
        _clear_cdp_refs()
        return block
    _clear_cdp_refs()
    try:
        await manager.ensure_connected()
        if wait_for_stable:
            await asyncio.sleep(0.75)
        aria_text = await manager.aria_snapshot()
        if not aria_text:
            return _json({"error": "No snapshot data available"})
        annotated, _ = _inject_refs_into_aria_snapshot(aria_text, browser_manager=_CDP_REF_MANAGER)
        visible_refs = set(re.findall(r"e\d+", annotated))
        _CDP_REF_MANAGER.restrict_ref_map(visible_refs)
        if mode == "interactive":
            snapshot_text = "\n".join(line for line in annotated.splitlines() if re.search(r"@e\d+", line))
        else:
            snapshot_text = annotated
            try:
                body_text = await manager.page.locator("body").inner_text(timeout=2000)
            except _CDP_OPERATION_ERRORS:
                body_text = ""
            if body_text:
                snapshot_text = "%s\n\nPage text preview:\n%s" % (
                    snapshot_text,
                    _truncate(str(body_text).replace("\r", " "), _CDP_SNAPSHOT_MAX_TEXT_CHARS),
                )
        selector_for_ref = {
            "@%s" % key.lstrip("@"): value for key, value in _CDP_REF_SELECTOR_MAP.items() if key.startswith("@")
        }
        important_controls = _cdp_important_controls_from_snapshot(snapshot_text)
        snapshot_original_chars = len(snapshot_text)
        # #579: compute page_health BEFORE truncation, off the full snapshot
        # text (which already includes the body-text preview above) so a
        # Cloudflare / JS-challenge interstitial on the desktop CDP surface
        # surfaces the same bot_protection fact the #575 loop halt consumes.
        page_title = await manager.get_title()
        page_health = detect_page_health(page_title, None, snapshot_text)
        snapshot_truncated = snapshot_original_chars > _CDP_SNAPSHOT_MAX_LLM_CHARS
        if snapshot_truncated:
            snapshot_text = _truncate(snapshot_text, _CDP_SNAPSHOT_MAX_LLM_CHARS)
        result = {
            "snapshot": snapshot_text.strip(),
            "url": await manager.get_url(),
            "title": page_title,
            "selector_for_ref": selector_for_ref,
            "refs_resolve_server_side": True,
            "important_controls": important_controls,
            "snapshot_truncated": snapshot_truncated,
            "snapshot_chars_original": snapshot_original_chars,
        }
        if page_health:
            result["page_health"] = page_health
        return _json(result)
    except _CDP_OPERATION_ERRORS as exc:
        return _json({"error": "Snapshot failed: %s" % exc})


async def _attach_cdp_interaction_snapshot(result: dict[str, Any]) -> None:
    """Attach fresh refs after an interaction invalidates the previous snapshot."""
    try:
        snapshot_payload = json.loads(await browser_snapshot(wait_for_stable=True, mode="interactive"))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug("Could not attach CDP interaction snapshot: %s", exc)
        return
    if not isinstance(snapshot_payload, dict):
        return
    if snapshot_payload.get("error") or snapshot_payload.get("ok") is False:
        if snapshot_payload.get("error"):
            result["snapshot_error"] = snapshot_payload.get("error")
        return

    result["snapshot"] = snapshot_payload.get("snapshot", "")
    result["selector_for_ref"] = snapshot_payload.get("selector_for_ref", {})
    result["refs_resolve_server_side"] = bool(snapshot_payload.get("refs_resolve_server_side"))
    result["important_controls"] = snapshot_payload.get("important_controls", [])
    result["snapshot_truncated"] = bool(snapshot_payload.get("snapshot_truncated"))
    result["snapshot_chars_original"] = int(snapshot_payload.get("snapshot_chars_original") or 0)
    if snapshot_payload.get("title"):
        result["title"] = snapshot_payload["title"]
    if snapshot_payload.get("url"):
        result["url"] = snapshot_payload["url"]


# ===========================================================================
# INTERACTION TOOLS
# ===========================================================================


async def _find_element_center(selector_or_text: str, text_filter: str = "") -> dict[str, Any]:
    """Resolve a selector or text to viewport center coordinates.

    Returns a dict with ``x``, ``y``, ``clicked_text``, ``match_count``
    on success, or ``error`` on failure.
    """
    try:
        ref_locator = await _resolve_cdp_ref_locator(selector_or_text)
    except ValueError:
        return {
            "error": "Unknown or stale snapshot ref '%s'." % selector_or_text,
            "stale_ref": True,
            "requested_ref": selector_or_text,
        }
    if ref_locator is not None:
        try:
            count = await ref_locator.count()
            if count == 0:
                return {
                    "error": "Ref '%s' no longer resolves on the current page." % selector_or_text,
                    "stale_ref": True,
                    "requested_ref": selector_or_text,
                }
            locator = ref_locator.first
            await locator.scroll_into_view_if_needed(timeout=5000)
            box = await locator.bounding_box(timeout=5000)
            if not box:
                return {"error": "Ref '%s' has no visible bounding box" % selector_or_text}
            try:
                clicked_text = await locator.inner_text(timeout=1000)
            except _CDP_OPERATION_ERRORS:
                clicked_text = ""
            try:
                href = await locator.evaluate(
                    "el => el.closest && el.closest('a[href]') ? el.closest('a[href]').href : ''"
                )
            except _CDP_OPERATION_ERRORS:
                href = ""
            return {
                "x": float(box["x"]) + float(box["width"]) / 2,
                "y": float(box["y"]) + float(box["height"]) / 2,
                "clicked_text": str(clicked_text).strip()[:80],
                "match_count": count,
                "ref": selector_or_text,
                "href": str(href or ""),
            }
        except _CDP_OPERATION_ERRORS as exc:
            return {"error": "Could not resolve ref '%s': %s" % (selector_or_text, exc)}

    resolved_selector = _resolve_cdp_selector(selector_or_text)
    if resolved_selector and resolved_selector != selector_or_text:
        selector_or_text = resolved_selector
    elif _is_cdp_ref(selector_or_text):
        return {
            "error": "Unknown or stale snapshot ref '%s'." % selector_or_text,
            "stale_ref": True,
            "requested_ref": selector_or_text,
        }
    if bogus := _bogus_selector_error(selector_or_text):
        return {"error": json.loads(bogus)["error"]}
    search_text = text_filter if text_filter else selector_or_text

    # Strategy 1: CSS selector
    if _is_css_selector(selector_or_text):
        result = await manager.evaluate_js(
            """((sel, textFilter) => {
                let els = Array.from(document.querySelectorAll(sel));
                if (textFilter) {
                    els = els.filter(el =>
                        (el.innerText || '').toLowerCase().includes(textFilter.toLowerCase())
                    );
                }
                if (els.length === 0) return { found: false };
                const el = els[0];
                el.scrollIntoView({block: 'center', inline: 'center'});
                const r = el.getBoundingClientRect();
                return {
                    found: true,
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    text: (el.innerText || el.value || '').trim().substring(0, 80),
                    count: els.length,
                    href: el.closest('a[href]') ? el.closest('a[href]').href : '',
                };
            })(%s, %s)"""
            % (
                json.dumps(selector_or_text),
                json.dumps(text_filter),
            )
        )
        if isinstance(result, dict) and result.get("found"):
            return {
                "x": result["x"],
                "y": result["y"],
                "clicked_text": result.get("text", ""),
                "match_count": result.get("count", 1),
                "selector": selector_or_text,
                "href": result.get("href", ""),
            }

    # Strategy 2: visible link text
    result = await manager.evaluate_js("""((searchText) => {
            const lowerSearch = searchText.toLowerCase();
            const links = Array.from(document.querySelectorAll('a[href], [role="link"]'));
            for (const el of links) {
                const t = (el.innerText || el.getAttribute('aria-label') || el.textContent || '').trim();
                if (t.toLowerCase().includes(lowerSearch)) {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        return {
                            found: true,
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2,
                            text: t.substring(0, 80),
                            href: el.href || '',
                            count: links.filter(link =>
                                (link.innerText || link.getAttribute('aria-label') || link.textContent || '')
                                    .trim().toLowerCase().includes(lowerSearch)
                            ).length,
                        };
                    }
                }
            }
            return { found: false };
        })(%s)""" % json.dumps(search_text))
    if isinstance(result, dict) and result.get("found"):
        return {
            "x": result["x"],
            "y": result["y"],
            "clicked_text": result.get("text", ""),
            "match_count": result.get("count", 1),
            "href": result.get("href", ""),
        }

    # Strategy 3: visible button text
    result = await manager.evaluate_js("""((searchText) => {
            const lowerSearch = searchText.toLowerCase();
            const btns = Array.from(document.querySelectorAll(
                'button, [role="button"], input[type="submit"], input[type="button"]'
            ));
            for (const el of btns) {
                const t = (el.innerText || el.value || '').trim();
                if (t.toLowerCase().includes(lowerSearch)) {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        return {
                            found: true,
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2,
                            text: t.substring(0, 80),
                        };
                    }
                }
            }
            return { found: false };
        })(%s)""" % json.dumps(search_text))
    if isinstance(result, dict) and result.get("found"):
        return {
            "x": result["x"],
            "y": result["y"],
            "clicked_text": result.get("text", ""),
            "match_count": 1,
        }

    # Strategy 4: generic visible text fallback
    result = await manager.evaluate_js("""((searchText) => {
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT, null
            );
            const lowerSearch = searchText.toLowerCase();
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.toLowerCase().includes(lowerSearch)) {
                    const el = node.parentElement;
                    if (el) {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            const anchor = el.closest ? el.closest('a[href]') : null;
                            return {
                                found: true,
                                x: r.left + r.width / 2,
                                y: r.top + r.height / 2,
                                text: (el.innerText || '').trim().substring(0, 80),
                                href: anchor ? anchor.href : '',
                            };
                        }
                    }
                }
            }
            return { found: false };
        })(%s)""" % json.dumps(search_text))
    if isinstance(result, dict) and result.get("found"):
        return {
            "x": result["x"],
            "y": result["y"],
            "clicked_text": result.get("text", ""),
            "match_count": 1,
            "href": result.get("href", ""),
        }

    candidates = await manager.evaluate_js("""((searchText) => {
            const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
            const query = norm(searchText);
            if (!query) return [];
            const words = query.split(' ').filter(Boolean);
            const els = Array.from(document.querySelectorAll(
                'a, button, input[type="submit"], input[type="button"], [role="button"], [role="link"]'
            ));
            return els.map((el) => {
                const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '').trim();
                const hay = norm(text);
                const score = hay.includes(query) ? 100 : words.filter((word) => hay.includes(word)).length;
                if (!score) return null;
                return {
                    text: text.substring(0, 120),
                    selector: el.id ? '#' + CSS.escape(el.id) : el.tagName.toLowerCase(),
                    href: el.href || '',
                    role: el.getAttribute('role') || el.tagName.toLowerCase(),
                    score,
                };
            }).filter(Boolean).sort((a, b) => b.score - a.score).slice(0, 8);
        })(%s)""" % json.dumps(search_text))
    payload: dict[str, Any] = {"error": "Could not find clickable element for '%s'" % selector_or_text}
    if isinstance(candidates, list) and candidates:
        payload["candidates"] = candidates
        payload["error"] += "; closest candidates are included"
    return payload


async def _do_click(selector: str, text: str = "") -> str:
    """Click an element via Playwright. Internal handler for browser_interact(action='click')."""
    try:
        await manager.ensure_connected()

        # Snapshot state before click
        url_before = await manager.get_url()
        fingerprint_before = await _cdp_page_fingerprint()
        field_count_before = await _cdp_form_field_count()

        # Find element and click
        target = await _find_element_center(selector, text)
        if "error" in target:
            payload = {
                "ok": False,
                "error": target["error"],
            }
            if target.get("candidates"):
                payload["candidates"] = target["candidates"]
            if target.get("stale_ref"):
                payload["stale_ref"] = True
                payload["requested_ref"] = target.get("requested_ref")
            return _json(payload)

        gate_info = await _cdp_element_info_at_point(float(target["x"]), float(target["y"]))
        if blocked := await _cdp_action_gate_block_from_info(
            gate_info,
            selector,
            text,
            str(target.get("clicked_text") or ""),
        ):
            return _json({"ok": False, "error": blocked})
        submit_control_click = _cdp_info_is_submit_control(gate_info)
        dom_click_fallback = False
        mutation_count_before = await _cdp_begin_click_effect_watch()

        await manager.click(target["x"], target["y"])

        if submit_control_click:
            changed = await _cdp_wait_for_click_effect(
                url_before,
                fingerprint_before,
                field_count_before,
                mutation_count_before,
            )
            if not changed:
                fallback_result = await _cdp_dom_click_target(
                    str(target.get("selector") or "") or None,
                    float(target["x"]),
                    float(target["y"]),
                )
                dom_click_fallback = bool(fallback_result.get("ok"))
                if dom_click_fallback:
                    await _cdp_wait_for_click_effect(
                        url_before,
                        fingerprint_before,
                        field_count_before,
                        mutation_count_before,
                    )
        else:
            # Wait for possible navigation / SPA render
            await asyncio.sleep(0.5)
            try:
                await manager.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except _CDP_OPERATION_ERRORS:
                logger.debug("domcontentloaded wait timed out after Playwright click")
            # Brief additional wait for SPA frameworks
            await asyncio.sleep(0.5)

        current_url = await manager.get_url()
        clicked_href = str(target.get("href") or "")
        href_followed = False
        href_follow_error = ""
        if not submit_control_click and clicked_href and current_url == url_before:
            href_url = clicked_href
            if href_url != url_before:
                href_follow_error = _validate_url(href_url) or ""
                if not href_follow_error:
                    try:
                        await manager.navigate(href_url)
                        _clear_cdp_refs()
                        href_followed = True
                        await asyncio.sleep(0.5)
                    except _CDP_OPERATION_ERRORS as exc:
                        href_follow_error = str(exc)

        title = await manager.get_title()
        current_url = await manager.get_url()
        result: dict[str, Any] = {
            "clicked": target.get("clicked_text") or selector,
            "title": title,
            "url": current_url,
        }
        if clicked_href:
            result["clicked_href"] = clicked_href
        if href_followed:
            result["href_followed"] = True
        elif href_follow_error:
            result["href_follow_error"] = href_follow_error
        if submit_control_click:
            result["submit_control_click"] = True
        if dom_click_fallback:
            result["dom_click_fallback"] = True

        mutation_count_after = await _cdp_click_mutation_count()
        dom_mutated_after_click = mutation_count_after > mutation_count_before
        if dom_mutated_after_click:
            result["dom_mutated_after_click"] = True
            result["dom_mutation_count"] = mutation_count_after - mutation_count_before

        focused_control_click = False
        if not submit_control_click and _cdp_info_is_focusable_form_control(gate_info):
            focused_control_click = await _cdp_click_focused_target(
                str(target.get("selector") or "") or None,
                float(target["x"]),
                float(target["y"]),
            )
            if focused_control_click:
                result["focused_control_click"] = True

        # Detect page changes
        if current_url != url_before:
            _clear_cdp_refs()
            result["navigated"] = True
            # Structured signal — same-tab navigation from click invalidates
            # @eN refs (parity with the Playwright server).
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "click_navigated"
        elif dom_mutated_after_click:
            _clear_cdp_refs()
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "click_dom_mutated"

        field_count_after = field_count_before
        try:
            field_count_after = await _cdp_form_field_count()
            if field_count_after != field_count_before:
                result["new_fields"] = field_count_after - field_count_before
                result["total_fields"] = field_count_after
                if not result.get("refs_invalidated"):
                    _clear_cdp_refs()
                    result["refs_invalidated"] = True
                    result["ref_invalidation_reason"] = "click_dom_mutated"
        except Exception:
            logger.debug("Could not count form fields after CDP click")

        # If significant page change, wait for network to settle
        field_delta = (field_count_after or 0) - (field_count_before or 0)
        if abs(field_delta) >= 3 or current_url != url_before:
            await asyncio.sleep(1.0)
            result["title"] = await manager.get_title()
            result["url"] = await manager.get_url()
            if result["url"] != url_before:
                _clear_cdp_refs()
                result["navigated"] = True
                result["refs_invalidated"] = True
                result["ref_invalidation_reason"] = "click_navigated"

        match_count = target.get("match_count", 1)
        if match_count > 1:
            result["match_count"] = match_count

        fingerprint_after = await _cdp_page_fingerprint()
        if (
            fingerprint_before
            and fingerprint_after
            and fingerprint_before == fingerprint_after
            and not result.get("navigated")
            and not focused_control_click
            and field_count_after == field_count_before
        ):
            no_effect_count = _record_cdp_no_effect(text or selector)
            result["ok"] = False
            result["last_click_no_effect"] = True
            result["semantic_no_effect"] = True
            result["no_effect_count"] = no_effect_count
            result["no_effect_target"] = text or selector
            result["snapshot_unchanged_after_click"] = True
            result["url_unchanged_after_click"] = True
            if dom_mutated_after_click:
                result["dom_mutation_only_click"] = True
            if no_effect_count >= 2:
                result["retry_blocked"] = True
                result["error"] = "Repeated click on '%s' had no visible effect." % (text or selector)
        else:
            _reset_cdp_no_effect_state()

        if result.get("refs_invalidated"):
            await _attach_cdp_interaction_snapshot(result)

        await _cdp_click_mutation_count(disconnect=True)
        return _json(result)
    except Exception as exc:
        return _json({"error": "Click failed: %s" % exc})


async def _do_type(selector: str, text: str, clear_first: bool = True) -> str:
    """Type text into an input field via Playwright. Internal handler for browser_interact(action='type')."""
    # Payment-card guard: refuse to type a literal PAN or MM/YY expiry into any
    # CDP-driven input.  Card data must flow through the dedicated payment
    # vault + fill_payment_details path, never through ``browser_interact``.
    if (violation := _payment_value_violation(text)) is not None:
        return _json({"ok": False, "error": violation})
    try:
        await manager.ensure_connected()
        if bogus := _bogus_selector_error(selector):
            return bogus
        locator = await _resolve_cdp_ref_locator(selector)
        if locator is None:
            resolved_selector = _resolve_cdp_selector(selector)
            if not resolved_selector:
                return _json({"ok": False, "error": "Unknown selector/ref '%s'" % selector})
            selector = resolved_selector
            locator = manager.page.locator(selector)
        else:
            selector = "@" + selector.lstrip("@")

        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, selector, text)
        if _field_block:
            return _json({"ok": False, "error": _field_block})
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, selector, text)
        if _pay_block:
            return _json({"ok": False, "error": _pay_block})

        try:
            tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
            input_type = ""
            if tag == "input":
                input_type = await locator.first.evaluate("el => (el.type || '').toLowerCase()", timeout=2000)
        except _CDP_OPERATION_ERRORS:
            tag = ""
            input_type = ""

        if not _is_text_value_verification_target(str(tag), str(input_type)) and str(tag) not in {"", "textarea"}:
            return _json({"ok": False, "error": "Unknown selector/ref '%s'" % selector})

        if clear_first:
            await _fill_text_locator_verified(locator, text, timeout=5000)
        else:
            await locator.first.focus(timeout=5000)
            press_sequentially = getattr(locator.first, "press_sequentially", None)
            if callable(press_sequentially):
                await press_sequentially(text, timeout=5000)
            else:
                await manager.type_text(text)

        # CDP-native post-fill verification (defense in depth, independent of
        # the Playwright-locator layer): read the element's actual ``.value``
        # straight off the live DOM via the raw CDP channel and confirm the
        # typed text genuinely persisted.  Mirrors the Playwright server's
        # "did not persist" surfacing in ``_fill_text_locator_verified``.  Only
        # runs for CSS-selector text targets — ``@eN`` refs are not resolvable
        # by ``document.querySelector`` so they fall through to the locator
        # verification already done above.
        if not selector.startswith("@") and _is_text_value_verification_target(str(tag), str(input_type)):
            verified, current = await _cdp_verify_text_value(selector, text)
            if not verified:
                return _json(
                    {
                        "ok": False,
                        "error": "value did not persist after fill",
                        "selector": selector,
                        "current_value": current,
                    }
                )

        return _json({"typed": text, "selector": selector, "cleared": clear_first})
    except Exception as exc:
        return _json({"error": "Type failed for '%s': %s" % (selector, exc)})


async def _do_select(selector: str, value: str) -> str:
    """Select a dropdown option via Playwright. Internal handler for browser_interact(action='select')."""
    if (violation := _payment_value_violation(value)) is not None:
        return _json({"ok": False, "error": violation})
    try:
        await manager.ensure_connected()
        if bogus := _bogus_selector_error(selector):
            return bogus
        locator = await _resolve_cdp_ref_locator(selector)
        if locator is None:
            resolved_selector = _resolve_cdp_selector(selector)
            if not resolved_selector:
                return _json({"ok": False, "error": "Unknown selector/ref '%s'" % selector})
            selector = resolved_selector
            locator = manager.page.locator(selector)
        else:
            selector = "@" + selector.lstrip("@")
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, selector, value)
        if _pay_block:
            return _json({"ok": False, "error": _pay_block})
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, selector)
        if _field_block:
            return _json({"ok": False, "error": _field_block})
        await _select_dropdown_value(locator, value, selector)
        return _json({"selected": value, "selector": selector})
    except Exception as exc:
        return _json({"error": "Select failed for '%s': %s" % (selector, exc)})


@server.tool(
    description=(
        "Primary tool for clicking, typing, and selecting elements on the visible desktop web page. "  # nosec B608 - model-facing prose, not SQL.
        "INPUT: action (required) + selector (CSS or visible text) + text (for 'type') or value (for 'select').\n"
        "SUCCESS: Returns JSON with action result, page title/URL. For clicks that change page state, "
        "includes navigated/refs_invalidated plus the fresh snapshot and selector_for_ref mapping.\n"
        "FAILURE: Element not found, multiple matches, or raw change-detection fields such as "
        "last_click_no_effect, no_effect_count, retry_blocked, navigated, and new_fields.\n"
        + _CDP_REF_USAGE_DOC
        + _CDP_SELECTOR_SYNTAX_DOC
        + "EXAMPLES:\n"
        "- browser_interact(action='click', selector='Add to Cart') -> clicks element with text 'Add to Cart'\n"
        "- browser_interact(action='click', selector='#submit-btn') -> clicks element by CSS selector\n"
        "- browser_interact(action='type', selector='#search', text='laptop') -> types 'laptop' into #search input\n"
        "- browser_interact(action='select', selector='#state', value='California') -> selects 'California' from dropdown"
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_interact(
    action: Annotated[
        str,
        Field(
            description=(
                "Interaction type. One of: "
                "'click' (click element by CSS selector or visible text), "
                "'type' (type text into input field by CSS selector), "
                "'select' (select dropdown option by CSS selector)."
            )
        ),
    ],
    selector: Annotated[
        str,
        Field(
            description=(
                "CSS selector or plain visible text to find the target element. "
                "Examples: '#submit-btn', 'button.primary', 'Add to Cart', "
                "'[name=\"email\"]', 'select[name=\"quantity\"]'."
                + _CDP_SELECTOR_SYNTAX_DOC
                + " If you have @eN from browser_snapshot, use selector_for_ref['@eN']; @eN is not a selector."
            )
        ),
    ] = "",
    text: Annotated[
        str,
        Field(
            description=(
                "Text to type for 'type' action. "
                "For 'click' action: optional text filter to disambiguate CSS selector matches."
            )
        ),
    ] = "",
    value: Annotated[
        str,
        Field(
            description=(
                "Option value or visible label text for 'select' action. "
                "Matches by value attribute first, then by label text."
            )
        ),
    ] = "",
    clear_first: Annotated[
        bool,
        Field(
            description=("For 'type' action only: whether to clear existing field content before typing. Default True.")
        ),
    ] = True,
) -> str:
    """Interact with browser elements using a single compound tool (CDP variant).

    Consolidates click, type, and select operations for CSS selectors.

    Examples:
    - browser_interact(action="click", selector="Add to Cart")
    - browser_interact(action="type", selector="#search", text="laptop")
    - browser_interact(action="select", selector="#state", value="California")
    """
    if action == "click":
        if not selector:
            return _json({"error": "selector is required for action='click'"})
        return await _do_click(selector, text)

    if action == "type":
        if not selector:
            return _json({"error": "selector is required for action='type'"})
        if not text:
            return _json({"error": "text is required for action='type'"})
        return await _do_type(selector, text, clear_first)

    if action == "select":
        if not selector:
            return _json({"error": "selector is required for action='select'"})
        if not value:
            return _json({"error": "value is required for action='select'"})
        return await _do_select(selector, value)

    return _json({"error": "Unknown action '%s'. Use one of: click, type, select" % action})


@server.tool(
    description=(
        "Fill multiple form fields in one call using @eN refs from browser_snapshot/browser_get_form_fields or CSS selectors."
        + _CDP_FILL_FORM_REF_DOC
        + _CDP_SELECTOR_SYNTAX_DOC
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_fill_form(fields: list[FormField]) -> str:
    """Fill input, textarea, checkbox, radio, and select fields in the visible CDP browser."""
    if not fields:
        return _json({"error": "fields list must not be empty"})

    filled: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for entry in fields:
        if isinstance(entry, dict):
            ref = str(entry.get("ref") or "").strip()
            value = str(entry.get("value") or "")
            is_select = bool(entry.get("select"))
        else:
            ref = str(entry.ref or "").strip()
            value = str(entry.value or "")
            is_select = bool(entry.select)

        if not ref:
            errors.append({"ref": ref, "error": "missing ref"})
            continue
        if (violation := _payment_value_violation(value)) is not None:
            return _json({"ok": False, "error": violation})

        try:
            locator = await _resolve_cdp_ref_locator(ref)
        except ValueError:
            errors.append({"ref": ref, "error": "unknown ref or unsupported selector"})
            continue
        if locator is None:
            selector = _resolve_cdp_selector(ref)
            if not selector:
                errors.append({"ref": ref, "error": "unknown ref or unsupported selector"})
                continue
            locator = manager.page.locator(selector)
        else:
            selector = "@" + ref.lstrip("@")
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, selector, value)
        if _field_block:
            return _json({"ok": False, "error": _field_block})
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, selector, value)
        if _pay_block:
            return _json({"ok": False, "error": _pay_block})
        try:
            tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
            input_type = ""
            if tag == "input":
                input_type = await locator.first.evaluate("el => (el.type || '').toLowerCase()", timeout=2000)
            if tag == "select" or is_select:
                await _select_dropdown_value(locator, value, selector)
                verified_value = value
            elif input_type == "checkbox":
                truthy = value.strip().lower() in {"true", "yes", "1", "checked", "on"}
                await locator.first.set_checked(truthy, timeout=5000)
                verified_value = str(truthy)
            elif input_type == "radio":
                await locator.first.check(timeout=5000)
                verified_value = "checked"
            else:
                await _fill_text_locator_verified(locator, value, timeout=5000)
                # CDP-native confirmation that the value reached the live DOM
                # element, independent of the Playwright-locator layer.  ``@eN``
                # refs are not resolvable by ``document.querySelector`` so they
                # rely on the locator verification above; CSS-selector targets
                # get the raw-CDP cross-check and surface "value did not persist".
                if not selector.startswith("@") and _is_text_value_verification_target(str(tag), str(input_type)):
                    persisted, current = await _cdp_verify_text_value(selector, value)
                    if not persisted:
                        errors.append(
                            {
                                "ref": ref,
                                "selector": selector,
                                "error": "value did not persist after fill (current=%r)" % current,
                            }
                        )
                        continue
                verified_value = value
            filled.append(
                {
                    "ref": ref,
                    "selector": selector,
                    "value": verified_value,
                    "verified": "value",
                }
            )
        except Exception as exc:
            errors.append({"ref": ref, "selector": selector, "error": str(exc)})
            continue

    return _json(
        {
            "ok": not errors,
            "filled_count": len(filled),
            "error_count": len(errors),
            "filled": filled,
            "errors": errors,
        }
    )


@server.tool(
    description=(
        "Press a keyboard key via Playwright keyboard actions. "
        "Handles Enter, Tab, Escape, arrow keys, Backspace/Delete, Space, and single characters. "
        "It presses one key at a time; key combinations are not supported. "
        "INPUT: key (string) — the key name. Supported special keys: 'Enter', 'Tab', 'Escape', "
        "'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', "
        "'PageUp', 'PageDown', 'Space'. Also accepts any single character like 'a', '5', '/'. "
        "Key names are case-insensitive ('enter' and 'Enter' both work). "
        "SUCCESS: Returns JSON with pressed (the key name). If Enter triggered a page navigation, also "
        "returns navigated_to with the new URL (waits up to 5 seconds for domcontentloaded). "
        "FAILURE: Returns error if the key press fails. If the key does not produce the expected "
        "effect, the focused element may not accept keyboard input."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def browser_press_key(key: str) -> str:
    """Press a keyboard key (Enter, Tab, Escape, etc.).

    Args:
        key: Key name (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').
    """
    try:
        await manager.ensure_connected()
        url_before = await manager.get_url()

        key_lower = key.lower()
        if gate_block := await _cdp_key_action_gate_block(key_lower):
            return _json({"ok": False, "error": gate_block})
        key_desc = _KEY_MAP.get(key_lower)

        if key_desc:
            await manager.press_key(str(key_desc["key"]))
        else:
            await manager.type_text(key)

        # If Enter, wait for possible navigation
        if key_lower in ("enter", "return"):
            try:
                await manager.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except (TimeoutError, Exception):
                logger.debug("domcontentloaded wait timed out after Playwright key press")

        result: dict[str, Any] = {"pressed": key}
        current_url = await manager.get_url()
        if current_url != url_before:
            result["navigated_to"] = current_url
            # Same-tab navigation from a key press invalidates @eN refs.
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "key_navigated"
        return _json(result)
    except Exception as exc:
        return _json({"error": "Key press failed for '%s': %s" % (key, exc)})


@server.tool(
    description=(
        "Scroll the page up or down via Playwright page JavaScript execution. "
        "This scrolls the main window only, not a specific scrollable div or iframe. "
        "INPUT: direction (string, default 'down') — either 'down' or 'up'. amount (int, default 3) — "
        "number of scroll units, where each unit is 400 pixels (roughly one viewport height). "
        "Examples: direction='down', amount=1 scrolls 400px down. direction='up', amount=5 scrolls "
        "2000px up. "
        "SUCCESS: Returns JSON with direction, amount, and scroll_y (final vertical scroll position in "
        "pixels). Waits 500ms after scrolling for lazy-loaded content to render. "
        "FAILURE: Returns error if the browser connection is lost. If scroll_y does not change after scrolling "
        "down, the page may already be at the bottom. If scroll_y is 0 after scrolling up, the page "
        "is at the top."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll the page.

    Args:
        direction: 'down' or 'up' (default 'down').
        amount: Number of viewport-heights to scroll (default 3).
    """
    try:
        await manager.ensure_connected()
        delta = amount * 400
        if direction.lower() == "up":
            delta = -delta
        await manager.evaluate_js("window.scrollBy(0, %d)" % delta)
        await asyncio.sleep(0.5)
        scroll_pos = await manager.evaluate_js("window.scrollY")
        return _json({"direction": direction, "amount": amount, "scroll_y": scroll_pos})
    except Exception as exc:
        return _json({"error": "Scroll failed: %s" % exc})


# ===========================================================================
# VISUAL TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Capture a PNG screenshot of the browser viewport for visual inspection. "
        "Returns image bytes for the viewport or full page. "
        "INPUT: full_page=false (default, viewport only) or full_page=true (entire scrollable page).\n"
        "SUCCESS: Returns {ok: true, title, url, image_base64, mime_type: 'image/png'}.\n"
        "FAILURE: Empty screenshot data or browser connection lost.\n"
        "EXAMPLES:\n"
        "- browser_screenshot() -> viewport screenshot as base64 PNG\n"
        "- browser_screenshot(full_page=true) -> full scrollable page screenshot"
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_screenshot(full_page: bool = False) -> str:
    """Take a screenshot and return it as an image for visual inspection.

    Uses Qt screenshot (OS-level fidelity) when available, falls back to
    browser screenshot.  The screenshot is returned as base64-encoded PNG data.

    Args:
        full_page: Capture full scrollable page (default False = viewport only).
            Note: full_page is only supported via the browser page, not Qt screenshots.
    """
    if block := await _browser_payment_observation_refusal("browser_screenshot"):
        return block
    try:
        await manager.ensure_connected()
        png_bytes = None

        # Try AgentPerception (prefers Qt, falls back to CDP)
        if manager.perception is not None and not full_page:
            png_bytes = await manager.perception.screenshot()
            if png_bytes and _black_screenshot_payload(png_bytes):
                logger.warning("Perception screenshot was black; falling back to CDP screenshot")
                png_bytes = None

        # Direct browser-page fallback
        if png_bytes is None:
            png_bytes = await manager.screenshot()

        if not png_bytes:
            return _json({"error": "Screenshot capture returned empty data"})

        blank_payload = _black_screenshot_payload(png_bytes)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        title = await manager.get_title()
        url = await manager.get_url()
        if blank_payload:
            blank_payload.update(
                {
                    "title": title,
                    "url": url,
                    "image_base64": b64,
                    "mime_type": "image/png",
                }
            )
            return _json(blank_payload)
        return _json(
            {
                "ok": True,
                "title": title,
                "url": url,
                "image_base64": b64,
                "mime_type": "image/png",
            }
        )
    except Exception as exc:
        return _json({"error": "Screenshot failed: %s" % exc})


# ===========================================================================
# ADVANCED TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Wait for a specific element to appear on the page via Playwright locator wait. "
        "This matches browser_wait_for from the Playwright server. "
        "It checks DOM element existence and visibility using CSS selectors. "
        "INPUT: selector (string) — CSS selector for the element to wait for. Examples: '.autocomplete-list', "
        "'#success-message', '.modal-dialog', '[data-loaded=\"true\"]', '.search-results li'. "
        + _CDP_SELECTOR_SYNTAX_DOC
        + "timeout (int, default 5000) — maximum wait time in milliseconds. Clamped to range 500-15000ms. "
        "The tool waits for Playwright's visible state. "
        "SUCCESS: Returns JSON with found=True, selector, count (number of matching elements), and "
        "text_preview (first 200 chars of the element's innerText). "
        "FAILURE: Returns found=False with a message if the element did not appear within the timeout. "
        "This is not an error; the page may still be loading."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_wait(selector: str, timeout: int = 5000) -> str:
    """Wait for an element to appear on the page.

    Useful after typing (autocomplete dropdowns), clicking (page transitions),
    or form submission (new content loading).

    Args:
        selector: CSS selector to wait for.
        timeout: Max wait time in milliseconds (default 5000).
    """
    try:
        await manager.ensure_connected()
        timeout = min(max(timeout, 500), 15000)  # clamp 0.5-15s
        locator = manager.page.locator(selector)
        try:
            await locator.first.wait_for(state="visible", timeout=timeout)
        except _CDP_OPERATION_ERRORS:
            return _json(
                {
                    "found": False,
                    "selector": selector,
                    "message": "Element did not appear within %dms" % timeout,
                }
            )
        try:
            text_preview = await locator.first.inner_text(timeout=1000)
        except _CDP_OPERATION_ERRORS:
            text_preview = ""
        try:
            count = await locator.count()
        except _CDP_OPERATION_ERRORS:
            count = 1
        return _json(
            {
                "found": True,
                "selector": selector,
                "count": count,
                "text_preview": str(text_preview)[:200],
            }
        )
    except Exception as exc:
        return _json({"error": "Wait failed: %s" % exc})


@server.tool(
    description=(
        "Execute a JavaScript expression in the visible browser page and return its result. "
        "Covers computed styles, shadow DOM, canvas state, custom JS APIs, inner-container scrolls, "
        "non-sensitive page state, DOM counts, and lookups that CSS selectors cannot express. "
        "Do not inspect cookies, browser storage, credentials, or auth/session tokens.\n"
        "INPUT: script (string) -- JS expression or IIFE returning a JSON-serializable value. Results truncated to 4000 chars.\n"
        "SUCCESS: Returns {result: <value>, url: '...'}.\n"
        "FAILURE: JS syntax error, element not found, or browser connection lost.\n"
        "EXAMPLES:\n"
        "- browser_evaluate(script=\"document.querySelectorAll('.item').length\") -> {result: 12, url: '...'}\n"
        "- browser_evaluate(script=\"document.querySelector('#price')?.textContent\") -> {result: '$29.99', url: '...'}"
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_evaluate(script: str) -> str:
    """Run JavaScript in the browser and return the result.

    The script should be a JS expression or an IIFE that returns a value.
    Useful for inspecting DOM state, finding hidden elements, or extracting
    data that other tools cannot access.

    Args:
        script: JavaScript code to evaluate. Must return a JSON-serializable value.
    """
    # Payment-card guard: refuse JS that writes a card number / expiry into the
    # page or sends one over the wire.  Card data must flow through the
    # dedicated payment vault + fill_payment_details path.
    if (violation := _js_payment_value_bypass(script)) is not None:
        return _json({"ok": False, "error": violation})
    if block := await _browser_payment_observation_refusal("browser_evaluate"):
        return block
    if blocked := blocked_js_pattern(script):
        return _json({"error": "Blocked JS pattern '%s' -- credential/storage access not permitted" % blocked})
    if gate_block := await _cdp_js_action_gate_block(script):
        logger.warning("Action gate blocked CDP browser_evaluate (script=%r)", script[:120])
        return _json({"ok": False, "error": gate_block})
    try:
        await manager.ensure_connected()
        if sig_block := await _cdp_signature_gate_block_from_current_page_action(script):
            logger.warning("Signature gate blocked CDP browser_evaluate (script=%r)", script[:120])
            return _json({"ok": False, "error": sig_block})
        result = await manager.evaluate_js(script)
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "...(truncated)"
            try:
                result = json.loads(result_str.rsplit(",", 1)[0] + "]")
            except (json.JSONDecodeError, ValueError):
                result = result_str
        url = await manager.get_url()
        return _json({"result": result, "url": url})
    except Exception as exc:
        return _json({"error": "Evaluate failed: %s" % exc})


async def _execute_overlay_playwright_line(line: str) -> str | None:
    stripped = line.strip().rstrip(";")
    if stripped == "page.url":
        return await manager.get_url()
    if stripped.startswith("page.") or stripped.startswith("asyncio.sleep"):
        stripped = "await " + stripped
    if stripped.startswith("await page.navigate("):
        stripped = stripped.replace("await page.navigate(", "await page.goto(", 1)
    return await _execute_playwright_line(manager.page, stripped)


@server.tool(
    description=(
        "Execute browser code. For simple DOM reads pass JavaScript directly, e.g. "
        "document.title, return document.title, evaluate(document.title), or evaluate document.title. "
        "For multi-step actions pass Playwright page.click/page.fill/page.evaluate lines. "
        "JavaScript exceptions return success=false instead of opaque success output."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_run_script(script: str) -> str:
    """Execute a JavaScript expression/read or a multi-step Playwright browser script."""
    if block := await _browser_payment_observation_refusal("browser_run_script"):
        return block
    if not script or not script.strip():
        return _json({"error": "script is required", "success": False})
    # Payment-card guard: refuse JS that writes a card number / expiry into the
    # page or sends one over the wire.  The same scan that protects
    # ``browser_evaluate`` applies to the whole script body, regardless of
    # which dispatch branch (raw JS vs. page.* lines) will run.
    if (violation := _js_payment_value_bypass(script)) is not None:
        return _json({"error": violation, "success": False})
    lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return _json({"error": "Script is empty (no executable lines)", "success": False})
    if gate_block := await _cdp_js_action_gate_block(script):
        logger.warning("Action gate blocked CDP browser_run_script (script=%r)", script[:200])
        return _json({"ok": False, "error": gate_block, "success": False})

    results: list[str] = []
    try:
        await manager.ensure_connected()
        if sig_block := await _cdp_signature_gate_block_from_current_page_action(script):
            logger.warning(
                "Signature gate blocked CDP browser_run_script (script=%r)",
                script[:200],
            )
            return _json({"ok": False, "error": sig_block, "success": False})
        if not any("page." in line or "asyncio.sleep" in line for line in lines):
            if blocked := blocked_js_pattern(script):
                return _json(
                    {
                        "error": "Blocked JS pattern '%s' -- credential/storage access not permitted" % blocked,
                        "success": False,
                    }
                )
            normalized_script = _normalize_browser_run_script_js(script)
            evaluated = await manager.evaluate_js(normalized_script)
            if error_text := _browser_eval_error_text(evaluated):
                return _json(
                    {
                        "ok": False,
                        "error": error_text,
                        "output": error_text,
                        "success": False,
                    }
                )
            return _json(
                {
                    "output": json.dumps(evaluated, ensure_ascii=False, default=str),
                    "success": True,
                }
            )

        for index, line in enumerate(lines, start=1):
            try:
                result = await _execute_overlay_playwright_line(line)
                results.append("Line %d OK: %s" % (index, line[:100]))
                if result:
                    results.append("  -> %s" % str(result)[:600])
            except Exception as exc:
                results.append("Line %d FAILED: %s" % (index, line[:100]))
                results.append("  Error: %s" % str(exc)[:400])
                snapshot = json.loads(await browser_snapshot(mode="interactive"))
                results.append("\nPage state at failure:\n%s" % snapshot.get("snapshot", ""))
                return _json(
                    {
                        "output": "\n".join(results),
                        "completed": index - 1,
                        "total": len(lines),
                        "success": False,
                    }
                )

        snapshot = json.loads(await browser_snapshot(mode="interactive"))
        results.append("\nAll %d lines executed successfully." % len(lines))
        if snapshot.get("snapshot"):
            results.append("\nFinal page state:\n%s" % snapshot["snapshot"])
        return _json(
            {
                "output": "\n".join(results),
                "completed": len(lines),
                "total": len(lines),
                "success": True,
            }
        )
    except Exception as exc:
        return _json({"error": "Script execution failed: %s" % exc, "success": False})


@server.tool(
    description=(
        "Return the current CDP page snapshot together with the requested assertion. "
        "No runtime pass/fail classifier is applied."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def verify_state(assertion: str) -> str:
    """Return current page state context for model-side assessment."""
    try:
        if not assertion or not assertion.strip():
            return _json({"error": "assertion is required"})
        snapshot = json.loads(await browser_snapshot(mode="full"))
        snapshot_text = str(snapshot.get("snapshot") or "")
        if not snapshot_text:
            return _json({"error": "No snapshot available for verification"})
        url = str(snapshot.get("url") or await manager.get_url())
        return _json({"assertion": assertion, "snapshot": snapshot_text, "url": url})
    except Exception as exc:
        return _json({"error": "Verification failed: %s" % exc})


# ===========================================================================
# SESSION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Check if the visible desktop browser connection is alive and get the current URL and page title. Lightweight liveness check. "
        "INPUT: No parameters.\n"
        "SUCCESS: Returns {running: true, url: '...', title: '...'} or {running: false, url: null, pages: 0}.\n"
        "FAILURE: Error only on unexpected exceptions. Disconnected browser = running: false, not an error.\n"
        "EXAMPLES:\n"
        "- browser_status() -> {running: true, url: 'https://google.com', title: 'Google'}\n"
        "- browser_status() -> {running: false, url: null, pages: 0}\n"
        "NOTE: If running=false, the next browser tool call auto-reconnects the Playwright CDP client."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_status() -> str:
    """Check browser status: running, current URL, page count."""
    try:
        if not manager.is_connected:
            return _json({"running": False, "url": None, "pages": 0})
        url = await manager.get_url()
        title = await manager.get_title()
        return _json({"running": True, "url": url, "title": title})
    except Exception as exc:
        return _json({"error": "Status check failed: %s" % exc})


@server.tool(
    description=(
        "Get HTTP requests and responses captured during the visible browser session for API discovery, returning the last 20 entries."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_api_log(domain_filter: str = "") -> str:
    """Get captured API calls from the current visible browser session."""
    if block := await _browser_payment_observation_refusal("browser_get_api_log"):
        return block
    try:
        if not manager.is_connected:
            return _json({"message": "No API calls captured in this session.", "count": 0})
        # Multi-tenant: the CDP-visible browser is desktop-only by design,
        # but we still scope the captured log by user_id so a future
        # shared-CDP topology cannot mix tenants.  Without an ambient
        # owner we return nothing rather than expose another tenant's
        # entries.
        try:
            from core.user_context import get_current_user_id

            owner = get_current_user_id()
        except Exception:
            owner = None
        if not owner:
            return _json({"message": "No API calls captured in this session.", "count": 0})
        log = await manager.get_api_log(domain_filter or None, user_id=owner)
        if not log:
            return _json({"message": "No API calls captured in this session.", "count": 0})
        entries = [format_api_log_entry(entry) for entry in log[-20:]]
        return _json({"count": len(log), "showing": len(entries), "entries": entries})
    except Exception as exc:
        return _json({"error": "Failed to get API log: %s" % exc})


@server.tool(
    description=(
        "Close the visible browser connection and release associated resources (Playwright connection and AgentPerception). "
        "This matches browser_close from the Playwright server. The next browser "
        "tool call automatically re-establishes the connection. "
        "INPUT: No parameters required. "
        "SUCCESS: Returns JSON with closed=True. The Playwright CDP connection is terminated and "
        "the AgentPerception instance is released. Any subsequent browser tool call will lazily "
        "reconnect by creating a new Playwright CDP client on the configured CDP port. "
        "FAILURE: Returns error if the close operation itself fails (rare). Even on failure, the internal "
        "state is reset so subsequent tool calls will attempt a fresh connection."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_close() -> str:
    """Close the browser connection. Next tool call will reconnect."""
    try:
        await manager.close()
        _clear_cdp_refs()
        _reset_cdp_no_effect_state()
        return _json({"closed": True})
    except Exception as exc:
        return _json({"error": "Close failed: %s" % exc})


@server.tool(
    description=(
        "Fail-safe visible-browser payment handoff. CDP exposes the tool so agents see the same surface, but secure saved-card fill still requires Playwright page adapter support."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "irreversible": True, "irreversible_class": "payment"},
)
async def fill_payment_details(card_label: str = "") -> str:
    """Do not expose card data in CDP; force the secure payment handoff."""
    return _json(
        {
            "ok": False,
            "code": "visible_browser_payment_fill_requires_takeover",
            "message": (
                "The visible CDP browser reached a payment-fill request, but saved-card DOM filling "
                'is not available on this path yet. Call payment(action="request_review", ...) '
                "so the user can complete the secure payment step in the visible browser."
            ),
            "required_tool": "payment",
            "required_action": "request_review",
            "card_label": card_label,
        }
    )


_MCP_SURFACE_REPLACEMENTS: dict[str, str] = {
    "browser_get_links": "browser_snapshot",
    "browser_get_form_fields": "browser_snapshot",
    "browser_get_page_info": "browser_snapshot",
    "browser_press_key": "browser_interact(action='press_key')",
    "browser_evaluate": "browser_run_script",
}


def _trim_overlapping_browser_tool_surface() -> None:
    """Keep legacy helpers importable while exposing the compact browser surface."""
    for tool_name in _MCP_SURFACE_REPLACEMENTS:
        server._tool_manager._tools.pop(tool_name, None)


_trim_overlapping_browser_tool_surface()


# ===========================================================================
# Factory
# ===========================================================================


def create_browser_cdp_server() -> FastMCP:
    """Return the configured CDP browser tools MCP server instance."""
    return server


def get_tool_count() -> int:
    """Return number of registered MCP tools."""
    return len(server._tool_manager._tools)
