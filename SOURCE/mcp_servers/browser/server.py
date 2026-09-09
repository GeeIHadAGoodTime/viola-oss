"""Playwright Browser MCP Server.

Exposes browser automation tools via MCP protocol using a separate-process
stdio transport.  Each tool delegates to a persistent ``BrowserManager``
that keeps a single Chromium instance alive across calls.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import ipaddress
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

try:
    from playwright.async_api import Error as PlaywrightError
except ImportError:
    PlaywrightError = RuntimeError

from core.constants import PAYMENT_GATE_MAX_OVERRIDES, TIMEOUT_5_MINUTES
from core.logging_config import get_logger
from services.browser.api_log_redaction import format_api_log_entry

from .browser_manager import BrowserManager
from .js_safety import blocked_js_pattern

# Shared safety helpers live in ``safety_helpers`` so the CDP browser server
# and this Playwright server use one copy of the SSRF blocklist, PAN redaction,
# and text utilities.  Re-exported here so ``from mcp_servers.browser.server
# import _validate_url`` (and the other names) keeps working for existing
# callers and tests.
from .safety_helpers import (
    _ALLOW_LOCALHOST,
    _ALLOWED_SCHEMES,
    _MAX_TEXT,
    _PAN_CANDIDATE_RE,
    _PAN_REDACTION_SKIP_KEYS,
    _PRIVATE_NETWORKS,
    _black_screenshot_payload,
    _http_error_page_payload,
    _is_css_selector,
    _js_payment_value_bypass,
    _json,
    _navigation_fact_fields,
    _normalize_navigation_url,
    _payment_value_violation,
    _redact_pan,
    _redact_pan_in_value,
    _truncate,
    _validate_url,
    detect_page_health,
)

logger = get_logger(__name__)
_BROWSER_OPERATION_ERRORS = (
    PlaywrightError,
    RuntimeError,
    TimeoutError,
    TypeError,
    ValueError,
)
_BROWSER_METADATA_PROBE_ERRORS = _BROWSER_OPERATION_ERRORS + (AttributeError,)
_BROWSER_SNAPSHOT_MAX_LLM_CHARS = 12_000


def _browser_disabled() -> str | None:
    """C5: Return a JSON error string if browser is disabled via env var, else None."""
    if os.environ.get("VIOLA_DISABLE_BROWSER", "").lower() in ("true", "1", "yes"):
        return json.dumps(
            {
                "ok": False,
                "error": "Browser automation is disabled. Set VIOLA_DISABLE_BROWSER=false to re-enable.",
            }
        )
    return None


# A4: Ref-format validation pattern — matches eN or @eN where N is digits.
_VALID_REF_RE = re.compile(r"^@?e\d+$")


def _validate_ref(ref: str) -> str | None:
    """A4: Validate ref format. Returns JSON error string if invalid, None if ok."""
    if not _VALID_REF_RE.match(ref.strip()):
        return json.dumps(
            {
                "ok": False,
                "error": "Invalid ref format '%s'. Expected 'eN' or '@eN' (e.g. e5, @e12)." % ref,
                "invalid_ref": ref,
                "expected_ref_format": "eN or @eN",
            }
        )
    return None


_SAFE = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_CONFIRM = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DANGEROUS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

_REF_USAGE_DOC = (
    " Snapshot refs such as @e2 are element refs, not CSS selectors. "
    "To use @e2, call browser_interact(action='click_ref', ref='e2'), "
    "browser_interact(action='fill_ref', ref='e2', text='...'), or "
    "browser_interact(action='select_ref', ref='e2', value='...'). "
    "Do not put @e2 in selector when a ref action is available."
)

_SELECTOR_SYNTAX_DOC = (
    " Selector arguments accept standard browser CSS selectors such as tags, #id, .class, "
    "[attr=value], combinators, and standard pseudo-classes like :not(), :nth-child(), "
    "and :checked. They do not accept jQuery/Sizzle text selectors such as :contains(), "
    ":icontains(), or :has(:contains(...)); Playwright-only :has-text() is also not "
    "valid in CSS selector fields. For visible text matching, use browser_snapshot @eN refs, "
    "browser_get_text then inspect the returned text, or browser_evaluate with DOM text matching/XPath."
)


class FormField(BaseModel):
    """A single form field to fill."""

    ref: str = Field(description="Element ref like e3 or @e3")
    value: str = Field(description="The text to type, or true/false for checkboxes")
    select: bool | None = Field(default=None, description="Set true for dropdown/combobox fields")


server = FastMCP("viola-browser")
manager = BrowserManager()

_ARIA_DROPDOWN_ROLES = {"listbox", "combobox"}
_SUBMIT_CLICK_WAIT_ATTEMPTS = 30
_SUBMIT_CLICK_WAIT_INTERVAL_MS = 400
_TEXT_INPUT_VERIFY_EXCLUDED_TYPES = {
    "button",
    "checkbox",
    "file",
    "hidden",
    "image",
    "radio",
    "reset",
    "submit",
}

_US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
_US_STATES_REVERSE: dict[str, str] = {v.lower(): k for k, v in _US_STATES.items()}


def _element_info_is_submit_control(info: dict[str, Any] | None) -> bool:
    """Return True for controls whose normal click should submit a form."""
    if not isinstance(info, dict):
        return False
    tag = str(info.get("tag") or "").strip().lower()
    input_type = str(info.get("type") or info.get("inputType") or "").strip().lower()
    if tag == "input" and input_type in {"submit", "image"}:
        return True
    if tag == "button" and input_type in {"", "submit"}:
        return True
    return False


async def _locator_submit_info(locator: Any) -> dict[str, Any]:
    try:
        info = await locator.first.evaluate(
            """el => ({
                tag: (el.tagName || '').toLowerCase(),
                type: (el.type || '').toLowerCase(),
                inputType: (el.type || '').toLowerCase(),
                value: ('value' in el && el.value != null) ? String(el.value) : '',
                text: el.innerText || el.textContent || '',
                role: (el.getAttribute('role') || '').toLowerCase()
            })""",
            timeout=1000,
        )
    except _BROWSER_METADATA_PROBE_ERRORS:
        return {}
    return info if isinstance(info, dict) else {}


async def _locator_is_submit_control(locator: Any) -> bool:
    return _element_info_is_submit_control(await _locator_submit_info(locator))


async def _playwright_page_fingerprint(page: Any) -> str:
    try:
        payload = await page.evaluate(
            """() => JSON.stringify({
                url: location.href,
                title: document.title || '',
                body: ((document.body && document.body.innerText) || '').substring(0, 5000),
                fields: Array.from(document.querySelectorAll('input, textarea, select')).map((el) => ({
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                    type: el.getAttribute('type') || el.tagName,
                    value: ('value' in el && el.value != null) ? String(el.value) : '',
                    checked: !!el.checked,
                    selectedIndex: typeof el.selectedIndex === 'number' ? el.selectedIndex : null
                }))
            })""",
        )
    except _BROWSER_OPERATION_ERRORS:
        return ""
    return " ".join(str(payload or "").split())


async def _wait_for_playwright_click_effect(page: Any, url_before: str, fingerprint_before: str) -> bool:
    for _ in range(_SUBMIT_CLICK_WAIT_ATTEMPTS):
        try:
            if page.url != url_before:
                return True
        except _BROWSER_OPERATION_ERRORS:
            logger.debug("Submit-click URL check failed")
        try:
            current = await _playwright_page_fingerprint(page)
            if fingerprint_before and current and current != fingerprint_before:
                return True
        except _BROWSER_OPERATION_ERRORS:
            logger.debug("Submit-click fingerprint check failed")
        try:
            await page.wait_for_timeout(_SUBMIT_CLICK_WAIT_INTERVAL_MS)
        except _BROWSER_OPERATION_ERRORS:
            await asyncio.sleep(_SUBMIT_CLICK_WAIT_INTERVAL_MS / 1000)
    return False


async def _click_submit_with_fallback(
    locator: Any,
    page: Any,
    url_before: str,
    fingerprint_before: str,
    *,
    timeout: int,
    force: bool = False,
) -> dict[str, Any]:
    await locator.first.click(timeout=timeout, **({"force": True} if force else {}))
    changed = await _wait_for_playwright_click_effect(page, url_before, fingerprint_before)
    if changed:
        return {"submit_control_click": True}
    try:
        await locator.first.evaluate(
            """el => {
                el.scrollIntoView({block: 'center', inline: 'center'});
                if (typeof el.click === 'function') el.click();
            }""",
            timeout=2000,
        )
    except _BROWSER_OPERATION_ERRORS:
        logger.debug("Submit DOM-click fallback failed")
        return {"submit_control_click": True}
    await _wait_for_playwright_click_effect(page, url_before, fingerprint_before)
    return {"submit_control_click": True, "dom_click_fallback": True}


def _is_text_value_verification_target(tag: str, input_type: str) -> bool:
    tag = (tag or "").strip().lower()
    input_type = (input_type or "").strip().lower()
    if tag == "textarea":
        return True
    if tag != "input":
        return False
    return input_type not in _TEXT_INPUT_VERIFY_EXCLUDED_TYPES


async def _verify_locator_text_value(locator: Any, expected: str) -> tuple[bool, str]:
    try:
        actual = await locator.first.evaluate(
            """el => ('value' in el && el.value != null) ? String(el.value) : ''""",
            timeout=2000,
        )
    except _BROWSER_OPERATION_ERRORS:
        logger.debug("Text-value verification read failed")
        return False, ""
    return str(actual) == str(expected), str(actual)


async def _fill_text_locator_verified(locator: Any, value: str, *, timeout: int) -> None:
    await locator.first.fill(value, timeout=timeout)
    verified, current = await _verify_locator_text_value(locator, value)
    if verified:
        return
    try:
        await locator.first.evaluate(
            """(el, val) => {
                el.focus();
                if ('value' in el) el.value = val;
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: val
                }));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
            timeout=2000,
        )
    except _BROWSER_OPERATION_ERRORS:
        logger.debug("Text-value JS persistence fallback failed")
    verified, current = await _verify_locator_text_value(locator, value)
    if not verified:
        raise RuntimeError("Field value did not persist after fill; current value is %r" % current)


_EVALUATE_PREFIX_RE = re.compile(r"^(?:await\s+)?(?:page\.)?evaluate\b(?P<tail>.*)$", re.DOTALL)
_JS_ERROR_CLASS_NAMES = {
    "Error",
    "EvalError",
    "RangeError",
    "ReferenceError",
    "SyntaxError",
    "TypeError",
    "URIError",
}


def _strip_single_call_parens(value: str) -> str:
    stripped = value.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return stripped
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(stripped):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(stripped) - 1:
                return stripped
    return stripped[1:-1].strip() if depth == 0 else stripped


def _unquote_js_string_literal(value: str) -> str:
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] not in {"'", '"'} or stripped[-1] != stripped[0]:
        return stripped
    try:
        decoded = ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return stripped
    return decoded if isinstance(decoded, str) else stripped


def _has_top_level_js_return(src: str) -> bool:
    """Return true when ``return`` appears in the script body, not nested JS."""
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    quote: str | None = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = 0

    while index < len(src):
        char = src[index]
        nxt = src[index + 1] if index + 1 < len(src) else ""

        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren = max(0, depth_paren - 1)
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace = max(0, depth_brace - 1)
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif (
            depth_paren == 0
            and depth_brace == 0
            and depth_bracket == 0
            and src.startswith("return", index)
            and (index == 0 or not (src[index - 1].isalnum() or src[index - 1] in {"_", "$"}))
            and (
                index + len("return") == len(src)
                or not (src[index + len("return")].isalnum() or src[index + len("return")] in {"_", "$"})
            )
        ):
            return True
        index += 1
    return False


def _normalize_browser_run_script_js(script: str) -> str:
    """Accept the natural JS/evaluate forms models try for single-expression reads."""
    src = script.strip()
    if src.endswith(";"):
        src = src[:-1].strip()
    match = _EVALUATE_PREFIX_RE.match(src)
    if match:
        tail = match.group("tail").strip()
        if tail.startswith("("):
            src = _strip_single_call_parens(tail)
        else:
            src = tail
        src = _unquote_js_string_literal(src)
    if _has_top_level_js_return(src):
        return "(async () => { %s })()" % src
    return src


def _browser_eval_error_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    class_name = str(value.get("className") or value.get("name") or "")
    description = str(value.get("description") or value.get("message") or value.get("error") or "")
    if value.get("subtype") == "error" or class_name in _JS_ERROR_CLASS_NAMES:
        return description or class_name or "JavaScript evaluation failed"
    if description and any(description.startswith("%s:" % error_name) for error_name in _JS_ERROR_CLASS_NAMES):
        return description
    return None


# Universal loading-state indicators.  After a click or snapshot, if
# any of these appear in the ARIA text the page likely has dynamic
# content still rendering.  A short wait + re-snapshot avoids giving
# the LLM a useless "Loading..." view.
_LOADING_PATTERNS = (
    "loading",
    "spinner",
    "updating",
    "please wait",
    "fetching",
    "processing",
    "loading cart",
    "loading order",
    "adding to cart",
)
_SNAPSHOT_TIME_RE = re.compile(
    r"\b(?:just now|today|yesterday|tomorrow|\d+\s*(?:s|sec|secs|second|seconds|"
    r"m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|"
    r"mo|mos|month|months|y|yr|yrs|year|years)\s+ago)\b",
    re.IGNORECASE,
)
_SNAPSHOT_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
_SNAPSHOT_DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.IGNORECASE,
)
_SNAPSHOT_COUNTER_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*[kmb]?\s*(?:likes?|comments?|shares?|followers?|following|views?|"
    r"reactions?|members?|posts?)\b",
    re.IGNORECASE,
)
_SNAPSHOT_VOLATILE_LINE_RE = re.compile(
    r"\b(?:sponsored|promoted|advertisement|suggested for you|ad choices?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Dead / parked page detection
# ---------------------------------------------------------------------------

# Known domain-parking service indicators (iframe src or page text).
# Bot-protection / parked-domain signal lists + the page-health detector moved
# to mcp_servers/browser/safety_helpers.py (#579) so the desktop CDP server can
# compute the same page_health facts the #575 loop halt consumes. Imported
# above as detect_page_health.


def _get_call_meta() -> dict[str, object]:
    """Return request metadata for the current browser MCP invocation."""
    try:
        context = server.get_context()
        request_context = context.request_context
        meta = getattr(request_context, "meta", None)
        if meta is None:
            return {}
        if hasattr(meta, "model_dump"):
            dumped = meta.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        if isinstance(meta, dict):
            return meta
    except Exception:
        logger.debug("No MCP request metadata available for browser tool call")
    return {}


def _get_call_user_id() -> str | None:
    """Extract the caller user_id from MCP request metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        user_id = user_context.get("user_id")
        if isinstance(user_id, str) and user_id:
            return user_id
    user_id = meta.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    return None


def _require_call_user_id(action: str) -> str:
    """Return the current MCP caller user_id or fail closed."""
    user_id = _get_call_user_id()
    if not user_id:
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            user_id = None
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("%s requires user_id" % action)
    resolved = user_id.strip()
    from core.user_context import is_legacy_local_user_id

    if is_legacy_local_user_id(resolved) or resolved == "desktop-local":
        raise ValueError("%s refuses sentinel user_id" % action)
    return resolved


def _get_call_gate_session_id() -> str | None:
    """Extract the browser-gate session id from MCP request metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        for key in ("gate_session_id", "session_id"):
            session_id = user_context.get(key)
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
    return None


def _extract_phone_browser_task_id(source: dict[str, Any]) -> str | None:
    for key in ("session_id", "gate_session_id"):
        task_id = source.get(key)
        if isinstance(task_id, str):
            task_id = task_id.strip()
            if task_id.lower().startswith("phone:"):
                return task_id
    return None


def _get_call_task_id() -> str | None:
    """Extract the cloud browser task id from MCP request metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        phone_task_id = _extract_phone_browser_task_id(user_context)
        if phone_task_id is not None:
            return phone_task_id
        for key in (
            "browser_task_id",
            "cloud_browser_task_id",
            "task_id",
            "gate_session_id",
            "session_id",
        ):
            task_id = user_context.get(key)
            if isinstance(task_id, str) and task_id.strip():
                return task_id.strip()
    phone_task_id = _extract_phone_browser_task_id(meta)
    if phone_task_id is not None:
        return phone_task_id
    for key in (
        "browser_task_id",
        "cloud_browser_task_id",
        "task_id",
        "gate_session_id",
        "session_id",
    ):
        task_id = meta.get(key)
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    return None


def _get_call_payment_gate_override_token() -> str | None:
    """Extract the code-issued one-use payment override token from MCP metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        token = user_context.get("payment_gate_override_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _get_call_payment_gate_active() -> bool:
    """Return whether the caller reports an active PAYMENT_GATE window."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if not isinstance(user_context, dict):
        return False
    active = user_context.get("payment_gate_active")
    if isinstance(active, bool):
        return active
    if isinstance(active, str):
        return active.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _get_call_payment_confirmation() -> dict[str, Any]:
    """Extract code-supplied payment confirmation metadata from MCP request metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if not isinstance(user_context, dict):
        return {}
    payment_confirmation = user_context.get("payment_confirmation")
    return payment_confirmation if isinstance(payment_confirmation, dict) else {}


manager.set_user_id_resolver(_get_call_user_id)
manager.set_task_id_resolver(_get_call_task_id)


def _normalize_dropdown_text(value: str | None) -> str:
    """Normalize dropdown values for fuzzy matching."""
    return (value or "").strip().lower()


def _dropdown_alternative_values(value: str) -> list[str]:
    """Return the original value plus common US state expansions."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        if not candidate:
            return
        normalized = _normalize_dropdown_text(candidate)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(candidate.strip())

    raw_value = value.strip()
    _add(raw_value)
    _add(_US_STATES.get(raw_value.upper()))
    _add(_US_STATES_REVERSE.get(raw_value.lower()))
    return candidates


async def _get_select_options(locator: Any) -> list[dict[str, str]]:
    """Return select option value/text pairs when available."""
    try:
        raw_options = await locator.first.evaluate(
            "el => Array.from(el.options || []).map(o => ({value: o.value || '', text: (o.text || '').trim()}))",
            timeout=2000,
        )
    except Exception:
        return []

    options: list[dict[str, str]] = []
    if not isinstance(raw_options, list):
        return options
    for raw_option in raw_options:
        if not isinstance(raw_option, dict):
            continue
        options.append(
            {
                "value": str(raw_option.get("value", "")),
                "text": str(raw_option.get("text", "")).strip(),
            }
        )
    return options


def _match_select_option(options: list[dict[str, str]], value: str) -> dict[str, str] | None:
    """Match a select option via exact, state-expanded, or partial text."""
    value_lower = _normalize_dropdown_text(value)
    if not value_lower:
        return None

    for option in options:
        option_value = _normalize_dropdown_text(option.get("value"))
        option_text = _normalize_dropdown_text(option.get("text"))
        if option_value == value_lower or option_text == value_lower:
            return option

    expanded = _US_STATES.get(value.strip().upper())
    if expanded:
        expanded_lower = expanded.lower()
        for option in options:
            option_value = _normalize_dropdown_text(option.get("value"))
            option_text = _normalize_dropdown_text(option.get("text"))
            if option_value == expanded_lower or option_text == expanded_lower:
                return option

    abbreviation = _US_STATES_REVERSE.get(value_lower)
    if abbreviation:
        abbreviation_lower = abbreviation.lower()
        for option in options:
            option_value = _normalize_dropdown_text(option.get("value"))
            option_text = _normalize_dropdown_text(option.get("text"))
            if option_value == abbreviation_lower or option_text == abbreviation_lower:
                return option

    for option in options:
        option_value = _normalize_dropdown_text(option.get("value"))
        option_text = _normalize_dropdown_text(option.get("text"))
        if (
            value_lower in option_text
            or option_text in value_lower
            or value_lower in option_value
            or option_value in value_lower
        ):
            return option
    return None


async def _apply_select_match(locator: Any, option: dict[str, str]) -> bool:
    """Select a matched option using its value, then its label."""
    option_value = str(option.get("value", ""))
    option_text = str(option.get("text", ""))
    if option_value:
        try:
            await locator.first.select_option(value=option_value, timeout=5000)
            return True
        except Exception:
            logger.debug("Dropdown value select failed")
    if option_text:
        try:
            await locator.first.select_option(label=option_text, timeout=5000)
            return True
        except Exception:
            logger.debug("Dropdown label select failed")
    return False


async def _try_click_aria_dropdown_option(locator: Any, value: str) -> bool:
    """Open an ARIA dropdown and click the best matching option."""
    try:
        tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
    except Exception:
        tag = ""
    if tag == "select":
        return False

    role = ""
    try:
        role = (await locator.first.get_attribute("role") or "").lower()
    except Exception:
        logger.debug("Dropdown role attribute lookup failed")
    if not role:
        try:
            role = await locator.first.evaluate("el => (el.getAttribute('role') || '').toLowerCase()", timeout=2000)
        except Exception:
            role = ""

    try:
        parent_role = await locator.first.evaluate(
            "el => (el.parentElement && el.parentElement.getAttribute('role') || '').toLowerCase()",
            timeout=2000,
        )
    except Exception:
        parent_role = ""

    if role not in _ARIA_DROPDOWN_ROLES and parent_role != "listbox":
        return False

    page = await manager.get_page()
    await locator.first.click(timeout=5000)
    await page.wait_for_timeout(150)

    for candidate in _dropdown_alternative_values(value):
        exact_pattern = re.compile(r"^\s*%s\s*$" % re.escape(candidate), re.IGNORECASE)
        partial_pattern = re.compile(re.escape(candidate), re.IGNORECASE)
        for pattern in (exact_pattern, partial_pattern):
            try:
                await page.get_by_role("option", name=pattern).first.click(timeout=3000)
                return True
            except Exception:
                logger.debug("ARIA dropdown role option click failed")
            try:
                await page.get_by_text(pattern).first.click(timeout=3000)
                return True
            except Exception:
                logger.debug("ARIA dropdown text option click failed")
    return False


async def _get_dropdown_label(locator: Any) -> str:
    """Best-effort dropdown label lookup for error messages."""
    try:
        label = await locator.first.evaluate(
            """el => {
                if (el.labels && el.labels[0]) return el.labels[0].textContent.trim();
                let prev = el.previousElementSibling;
                if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
                return el.getAttribute('aria-label') || el.getAttribute('name') || '';
            }""",
            timeout=2000,
        )
    except Exception:
        return ""
    return label if isinstance(label, str) else ""


async def _select_dropdown_value(locator: Any, value: str, ref: str) -> None:
    """Select a dropdown value using exact and fuzzy fallbacks."""
    try:
        await locator.first.select_option(value=value, timeout=5000)
        return
    except Exception:
        logger.debug("Dropdown exact value select failed")

    try:
        await locator.first.select_option(label=value, timeout=5000)
        return
    except Exception:
        logger.debug("Dropdown exact label select failed")

    options = await _get_select_options(locator)
    matched = _match_select_option(options, value)
    if matched and await _apply_select_match(locator, matched):
        return

    if await _try_click_aria_dropdown_option(locator, value):
        return

    hint_options = [option["text"] for option in options[:10] if option.get("text")]
    hint = " Options: %s" % ", ".join(hint_options) if hint_options else ""
    label = await _get_dropdown_label(locator)
    label_hint = " (%s)" % label if label else ""
    raise ValueError("Dropdown '%s'%s has no option matching '%s'.%s" % (ref, label_hint, value, hint))


# ---------------------------------------------------------------------------
# New-element tracking & post-click validation state
# ---------------------------------------------------------------------------

# Tracks element identities (role + label) from the previous snapshot so we
# can mark newly-appeared elements with a * prefix (Browser Use pattern).
_PREV_SNAPSHOT_IDENTITIES_BY_USER: dict[str, set[str]] = {}


# Stores the last snapshot text (ref-stripped) for post-click comparison
# (Skyvern Validator pattern — detect clicks that had no visible effect).
@dataclass
class _HintState:
    """Per-page hint state for duplicate-page and no-effect detection."""

    last_snapshot_fingerprint: str = ""
    last_no_effect_ref: str = ""
    last_no_effect_identity: str = ""
    last_no_effect_count: int = 0
    last_navigate_url: str = ""
    last_touch_monotonic: float = 0.0


_HINT_STATE_BY_PAGE_ID: dict[tuple[str, int], _HintState] = {}
_HINT_STATE_IDLE_RESET_SECONDS = 120.0

# Tracks consecutive no-effect clicks on the same ref. Any no-effect click
# returns ok:false; a repeated no-effect click also sets retry_blocked.
# Evidence: task c815efb96bd2 — 8x @e13 click, page never changed.
# Spin detector fired text injection at step 10, model ignored it.

# Tracks the last URL passed to browser_navigate for same-URL detection.
# Evidence: 7 tasks with repeated navigates to identical URLs — model
# navigates 3-5x to same page without acting on it (tasks dd1f9ae44329,
# f42d87b75bfb, b48a1f2e8393, ce42268bb3fc, a24c3cb54b4e, b44a93e09413).

# Regex matching a ref line to extract: indent+dash, role, and rest (label).
# Used by _mark_new_elements to compute element identity without ref numbers.
_REF_IDENTITY_RE = re.compile(r"^(\s*-\s+)(\w+)\s+@e\d+\s+(.*)$")

# Strip @eN refs for content-only comparison (post-click validation).
_REF_STRIP_RE = re.compile(r"@e\d+\s*")


def _get_state_user_id(user_id: str | None = None) -> str:
    """Resolve the current browser-state user_id without inventing a fallback."""
    try:
        return manager._resolve_user_id(user_id)
    except (RuntimeError, ValueError):
        if user_id and str(user_id).strip():
            return str(user_id).strip()
        from core.user_context import get_device_user_id

        return get_device_user_id()


def _reset_hint_state(page: Any | None = None, user_id: str | None = None) -> None:
    """Reset hint-state tracking for one page or all pages for a user."""
    resolved_user_id = _get_state_user_id(user_id)
    if page is None:
        stale_keys = [key for key in _HINT_STATE_BY_PAGE_ID if key[0] == resolved_user_id]
        for key in stale_keys:
            _HINT_STATE_BY_PAGE_ID.pop(key, None)
        _PREV_SNAPSHOT_IDENTITIES_BY_USER.pop(resolved_user_id, None)
    else:
        _HINT_STATE_BY_PAGE_ID.pop((resolved_user_id, id(page)), None)


def _get_hint_state(page: Any, user_id: str | None = None) -> _HintState:
    """Return the hint state for *page*, resetting stale entries."""
    page_key = (_get_state_user_id(user_id), id(page))
    now = time.monotonic()
    state = _HINT_STATE_BY_PAGE_ID.get(page_key)
    if state is None or (
        state.last_touch_monotonic and now - state.last_touch_monotonic > _HINT_STATE_IDLE_RESET_SECONDS
    ):
        state = _HintState()
        _HINT_STATE_BY_PAGE_ID[page_key] = state
    state.last_touch_monotonic = now
    return state


def _reset_no_effect_state(state: _HintState) -> None:
    state.last_no_effect_ref = ""
    state.last_no_effect_identity = ""
    state.last_no_effect_count = 0


def _no_effect_identity(ref: str, metadata: dict[str, Any]) -> str:
    """Return a stable click identity that survives @eN renumbering."""
    role = " ".join(str(metadata.get("role") or "").strip().lower().split())
    name = " ".join(str(metadata.get("name") or "").strip().lower().split())
    if role or name:
        return "%s:%s" % (role, name)
    return ref


# Matches lines that contain a ref AND some text content (button or link).
_REF_LINE_RE = re.compile(r"@e\d+")


def _fill_form_button_candidates(snapshot: str | None) -> list[dict[str, str]]:
    """Return objective Fill Form/Auto-fill/Populate button candidates from a snapshot."""
    if not snapshot:
        return []
    candidates: list[dict[str, str]] = []
    for match in re.finditer(
        r'button\s+@?(e\d+)\s+"([^"]*(?:Fill\s*Form|Auto-?fill|Populate)[^"]*)"',
        snapshot,
        re.IGNORECASE,
    ):
        candidates.append({"ref": "@%s" % match.group(1).lstrip("@"), "label": match.group(2)})
    return candidates


def _check_dead_page(
    title: str | None,
    description: str | None,
    snapshot: str | None,
) -> dict[str, Any] | None:
    """Return objective page-health facts for empty, parked, or blocked pages.

    Thin wrapper over the shared ``detect_page_health`` (safety_helpers) so the
    Playwright and CDP servers cannot drift. The Playwright server passes its
    richer ``_has_meaningful_content`` so a ref-less-but-meaningful ARIA tree is
    not misreported as empty.
    """
    return detect_page_health(
        title,
        description,
        snapshot,
        meaningful_content_check=_has_meaningful_content,
    )


async def _post_navigate_ssrf_check(page, private_networks: list) -> str | None:
    """Re-validate page URL after navigation to catch DNS rebinding.

    Playwright resolves DNS independently from our pre-navigation check,
    so a DNS rebinding attack could return a safe IP first (our check) and
    a private IP second (Playwright's actual request).  Re-resolving the
    final URL and checking again closes this TOCTOU window.
    """
    try:
        final_url = page.url
        if final_url and final_url not in ("about:blank", ""):
            parsed = urlparse(final_url)
            if parsed.hostname:
                addrs = socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or 443,
                    proto=socket.IPPROTO_TCP,
                )
                for _family, _type, _proto, _canonname, sockaddr in addrs:
                    ip = ipaddress.ip_address(sockaddr[0])
                    for net in private_networks:
                        if ip in net:
                            await page.goto("about:blank")
                            return "Blocked: navigation resolved to private IP %s" % ip
    except Exception:
        logger.debug("Private-IP DNS check failed")
    return None


def _browser_text_limit(max_chars: int | None) -> int:
    try:
        from config.settings import settings

        default_chars = int(getattr(settings, "browser_get_text_default_chars", 8000) or 8000)
        max_allowed = int(getattr(settings, "browser_get_text_max_chars", 20000) or 20000)
    except Exception:
        default_chars = _MAX_TEXT
        max_allowed = 20000

    requested = default_chars if max_chars is None else max_chars
    try:
        requested_int = int(requested)
    except (TypeError, ValueError):
        requested_int = default_chars
    return min(max(1, requested_int), max(1, max_allowed))


def _text_slice_payload(text: str, *, offset: int = 0, max_chars: int | None = None) -> dict[str, Any]:
    try:
        normalized_offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        normalized_offset = 0
    limit = _browser_text_limit(max_chars)
    total_chars = len(text)
    chunk = text[normalized_offset : normalized_offset + limit]
    next_offset = normalized_offset + len(chunk)
    has_more = next_offset < total_chars
    return {
        "text": chunk,
        "offset": normalized_offset,
        "returned_chars": len(chunk),
        "total_chars": total_chars,
        "next_offset": next_offset if has_more else None,
        "truncated": has_more,
    }


def _payment_gate_observation_refusal(tool_name: str) -> str | None:
    if not _get_call_payment_gate_active():
        return None
    if _get_call_payment_gate_override_token():
        return None
    message = (
        "Payment gate is active. Browser page inspection is paused while secure "
        "payment confirmation is pending. Use the hosted confirmation page; "
        "post-confirmation submit agents may proceed with a payment gate override."
    )
    return _json(
        {
            "ok": False,
            "code": "payment_gate_active",
            "message": message,
            "error": message,
            "tool": tool_name,
        }
    )


async def _browser_payment_observation_refusal(tool_name: str, page: Any | None = None) -> str | None:
    from services.payments.browser_payment_guard import (
        browser_payment_block_payload,
        browser_payment_sensitive_active_for_page,
    )

    if not await browser_payment_sensitive_active_for_page(page):
        return None
    return _json(browser_payment_block_payload(tool_name))


def _auto_screenshot_enabled() -> bool:
    """Check if auto-screenshot is enabled (VIOLA_AUTO_SCREENSHOT env var, default '1')."""
    return os.environ.get("VIOLA_AUTO_SCREENSHOT", "1") == "1"


async def _enrich_auto_screenshot(page: Any, result: dict[str, Any]) -> None:
    """Add a PNG screenshot to *result* dict if auto-screenshot is enabled.

    Mutates *result* in-place by adding ``image_base64`` and ``mime_type``.
    Silently does nothing on failure or when disabled.
    """
    if not _auto_screenshot_enabled():
        return
    if not result.get("snapshot"):
        return  # Only attach to results that have an ARIA snapshot
    if await _browser_payment_observation_refusal("auto_screenshot", page):
        return
    try:
        import base64

        png_bytes = await page.screenshot()
        result["image_base64"] = base64.b64encode(png_bytes).decode("ascii")
        result["mime_type"] = "image/png"
    except Exception:
        logger.debug("Auto-screenshot capture failed, continuing without it")


# jQuery-style pseudo-selectors that look like CSS but are not valid in
# Playwright/browser CSS engines. Detect them up-front so failed calls return
# the selector facts instead of timing out.
_BOGUS_PSEUDO_RE = re.compile(
    r"(?ix)"  # case-insensitive, verbose
    r"(?:"
    r":contains\s*\("  # jQuery :contains(...)
    r"|:has-text\s*\("  # Playwright-only when used in vanilla CSS context
    r"|:icontains\s*\("  # jQuery extension
    r"|\[\s*innerText\s*[*^$~|]?=\s*"  # [innerText="..."]
    r"|\[\s*textContent\s*[*^$~|]?=\s*"  # [textContent="..."]
    r"|\[\s*innerHTML\s*[*^$~|]?=\s*"  # [innerHTML="..."]
    r")"
)


def _bogus_selector_error(selector: str) -> str | None:
    """Return a JSON error string when *selector* uses jQuery-style pseudos.

    We fail fast instead of letting Playwright spend time on invalid selectors.
    """
    if not selector:
        return None
    if _BOGUS_PSEUDO_RE.search(selector):
        return _json(
            {
                "ok": False,
                "error": (
                    "Selector '%s' uses jQuery-style pseudo-selectors "
                    "(:contains, [innerText=...], etc.) that are not valid CSS." % selector[:120]
                ),
                "invalid_selector_syntax": True,
                "selector": selector[:120],
                "invalid_patterns": [":contains", "[innerText=...]", "[textContent=...]", "[innerHTML=...]"],
            }
        )
    return None


# ---------------------------------------------------------------------------
# Accessibility tree serialization helpers
# ---------------------------------------------------------------------------

# Roles that carry no semantic value — skip them but still recurse into children.
_SKIP_ROLES = frozenset({"none", "presentation", "generic", "paragraph", ""})

# Roles that represent interactive elements and receive @eN refs.
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "checkbox",
        "radio",
        "combobox",
        "menuitem",
        "tab",
        "switch",
        "slider",
        "spinbutton",
        "searchbox",
        "option",
        "menuitemcheckbox",
        "menuitemradio",
        "listbox",
        "treeitem",
    }
)

# Roles that indicate meaningful content inside an iframe (not tracking
# pixels or ad containers).  Used by _has_meaningful_content() to decide
# whether an iframe's ARIA tree is worth including in the snapshot.
_MEANINGFUL_ROLES = _INTERACTIVE_ROLES | frozenset(
    {
        "heading",
        "table",
        "form",
        "list",
        "navigation",
        "main",
        "article",
    }
)


def _has_meaningful_content(aria_text: str) -> bool:
    """Return True if *aria_text* has at least one meaningful ARIA role.

    Filters out empty iframes, tracking pixels, and ad containers.
    """
    if not aria_text or not aria_text.strip():
        return False
    line_re = re.compile(r"^\s*-\s+(\w+)")
    for line in aria_text.split("\n"):
        m = line_re.match(line)
        if m and m.group(1) in _MEANINGFUL_ROLES:
            return True
    return False


def _inject_refs_into_aria_snapshot(
    aria_text: str,
    browser_manager: BrowserManager | None = None,
    ref_start: int = 0,
    frame: Any = None,
) -> tuple[str, int]:
    """Inject ``@eN`` refs into an ARIA snapshot string.

    Playwright's ``aria_snapshot()`` returns lines like::

        - heading "Example Domain" [level=1]
        - link "More information..."
        - textbox "Customer name:"
        - button "Submit"

    This function parses each line, detects the role, and injects ``@eN``
    refs for interactive elements so the LLM can use ``browser_interact(action='click_ref')``,
    ``browser_fill_form``, etc.

    Result::

        - heading "Example Domain" [level=1]
        - link @e1 "More information..."
        - textbox @e2 "Customer name:"
        - button @e3 "Submit"

    Args:
        aria_text: The ARIA snapshot text to annotate.
        browser_manager: BrowserManager to register refs with.
        ref_start: Starting ref counter (for continuing numbering across frames).
        frame: Playwright Frame that owns these elements (None = main page).

    Returns:
        Tuple of (annotated text, final ref counter).
    """
    if not aria_text:
        return aria_text, ref_start

    # Regex to parse ARIA snapshot lines: captures leading whitespace + dash,
    # the role name, and the rest (quoted name + optional attributes).
    # Matches: "  - button "Submit"" -> ("  - ", "button", ' "Submit"')
    line_re = re.compile(r"^(\s*-\s+)(\w+)(.*)$")

    ref_counter = ref_start
    out_lines: list[str] = []
    # Track the absolute index of each role for unnamed element resolution.
    # e.g. role_indices["textbox"] = 3 means we've seen 3 textboxes so far.
    role_indices: dict[str, int] = {}
    # Track index among same (role, name) peers for disambiguation.
    # Responsive sites often have duplicate nav links (mobile + desktop)
    # with identical role+name.  Without this, resolve_ref matches the
    # wrong one.
    role_name_indices: dict[tuple[str, str], int] = {}

    for line in aria_text.split("\n"):
        m = line_re.match(line)
        if not m:
            # Non-matching lines (text nodes, empty) pass through unchanged.
            out_lines.append(line)
            continue

        prefix = m.group(1)  # e.g. "  - "
        role = m.group(2)  # e.g. "button"
        rest = m.group(3)  # e.g. ' "Submit"' or ' "More info":' (with children colon)

        if role in _INTERACTIVE_ROLES and browser_manager is not None:
            # Track absolute index per role (needed for unnamed element resolution).
            role_idx = role_indices.get(role, 0)
            role_indices[role] = role_idx + 1

            # Extract the quoted name for ref resolution.
            name_match = re.search(r'"([^"]*)"', rest)
            name = name_match.group(1) if name_match else ""

            # Track index among peers with same role+name (for nth() disambiguation).
            rn_key = (role, name)
            name_idx = role_name_indices.get(rn_key, 0)
            role_name_indices[rn_key] = name_idx + 1

            ref_counter += 1
            ref_id = "e%d" % ref_counter
            browser_manager.set_ref(
                ref_id,
                role,
                name,
                frame=frame,
                role_index=role_idx,
                name_index=name_idx,
            )
            # Inject @eN between the role and the quoted name.
            out_lines.append("%s%s @%s%s" % (prefix, role, ref_id, rest))
            continue

        out_lines.append(line)

    return "\n".join(out_lines), ref_counter


async def _build_iframe_snapshot(
    frame: Any,
    browser_mgr: BrowserManager,
    ref_counter: int,
    is_main: bool = False,
) -> tuple[str | None, int]:
    """Recursively build an ARIA snapshot that includes iframe content.

    For each frame, takes its ARIA snapshot, injects ``@eN`` refs, then
    recursively processes child iframes and merges their content into the
    corresponding ``- iframe`` nodes in the parent tree.

    Args:
        frame: Playwright Frame to snapshot.
        browser_mgr: BrowserManager for ref registration.
        ref_counter: Current ref counter (globally unique across all frames).
        is_main: True for the top-level main frame.

    Returns:
        Tuple of (annotated snapshot text or None, updated ref counter).
    """
    # Take this frame's ARIA snapshot.
    try:
        aria_text = await frame.locator("body").aria_snapshot()
    except Exception:
        if not is_main:
            return None, ref_counter
        # Main frame: continue with empty text so child frames are still processed.
        aria_text = ""

    if not aria_text:
        if not is_main:
            return None, ref_counter
        # Main frame body is empty (e.g. frameset or minimal iframe wrapper).
        # Continue to process child frames — their content IS the page content.

    # For non-main frames, skip if content is not meaningful (ads, trackers).
    if not is_main and not _has_meaningful_content(aria_text):
        return None, ref_counter

    # Inject @eN refs.  Main-frame refs store frame=None so resolve_ref()
    # uses page; iframe refs store their Frame object for scoped resolution.
    frame_ref = None if is_main else frame
    annotated, ref_counter = _inject_refs_into_aria_snapshot(
        aria_text,
        browser_manager=browser_mgr,
        ref_start=ref_counter,
        frame=frame_ref,
    )

    # Recursively process child iframes.
    child_frames = getattr(frame, "child_frames", None) or []
    if not child_frames:
        # For main frame with empty body and no children, return None.
        return (annotated or None), ref_counter

    # Build snapshot for each child frame.
    child_results: list[tuple[Any, str | None]] = []
    for child in child_frames:
        try:
            child_snap, ref_counter = await _build_iframe_snapshot(
                child,
                browser_mgr,
                ref_counter,
                is_main=False,
            )
        except Exception:
            child_snap = None
        child_results.append((child, child_snap))

    # Find "- iframe" lines in the annotated snapshot and merge children.
    lines = annotated.split("\n") if annotated else []
    iframe_indices: list[int] = []
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped == "- iframe" or stripped.startswith("- iframe ") or stripped.startswith("- iframe:"):
            iframe_indices.append(i)

    result_lines: list[str] = []
    child_idx = 0
    for i, ln in enumerate(lines):
        if i in iframe_indices and child_idx < len(child_results):
            _child_frame, child_snap = child_results[child_idx]
            child_idx += 1
            if child_snap:
                # Indent child content under the iframe node.
                indent = len(ln) - len(ln.lstrip())
                child_indent = " " * (indent + 2)
                iframe_line = ln.rstrip()
                if not iframe_line.endswith(":"):
                    iframe_line += ":"
                result_lines.append(iframe_line)
                for cl in child_snap.split("\n"):
                    if cl.strip():
                        result_lines.append(child_indent + cl)
            # else: empty/inaccessible iframe — omit the line
        else:
            # Skip empty lines from an empty main-frame body.
            if ln.strip() or lines != [""]:
                result_lines.append(ln)

    # Append any remaining child snapshots that didn't match iframe lines.
    while child_idx < len(child_results):
        child_frame_obj, child_snap = child_results[child_idx]
        child_idx += 1
        if child_snap:
            url = ""
            try:
                url = child_frame_obj.url or ""
            except Exception:
                logger.debug("Could not read iframe URL, using generic label")
            label = url[:80] if url and url != "about:blank" else "embedded content"
            result_lines.append('- group "[iframe: %s]":' % label)
            for cl in child_snap.split("\n"):
                if cl.strip():
                    result_lines.append("  " + cl)

    final = "\n".join(result_lines)
    return final if final.strip() else None, ref_counter


# ---------------------------------------------------------------------------
# Snapshot enrichment — new-element markers, interactive filter, fingerprint
# ---------------------------------------------------------------------------


def _mark_new_elements(snapshot: str, user_id: str | None = None) -> str:
    """Prefix newly-appeared interactive elements with ``*`` marker.

    Inspired by Browser Use's new-element detection: compares the current
    snapshot's interactive elements (by role + label, ignoring ref numbers)
    against the previous snapshot.  Elements not seen before get a ``*``
    prefix so the LLM can focus on what changed after a page interaction::

        - *button "Place Your Order"      ← new
        - link @e3 "Home"                 ← seen before

    On the first snapshot (no prior state), no markers are added.
    """
    resolved_user_id = _get_state_user_id(user_id)
    previous = _PREV_SNAPSHOT_IDENTITIES_BY_USER.get(resolved_user_id, set())
    current: set[str] = set()
    lines = snapshot.split("\n")
    out: list[str] = []

    for line in lines:
        m = _REF_IDENTITY_RE.match(line)
        if m:
            # Identity = role + label (ref number changes every snapshot)
            identity = "%s %s" % (m.group(2), m.group(3).rstrip())
            current.add(identity)
            if previous and identity not in previous:
                # New element — prefix the role with *
                indent = m.group(1)
                rest = line[len(indent) :]
                out.append("%s*%s" % (indent, rest))
                continue
        out.append(line)

    _PREV_SNAPSHOT_IDENTITIES_BY_USER[resolved_user_id] = current
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Explicit unchecked state for checkboxes
# ---------------------------------------------------------------------------
_CHECKBOX_LINE_RE = re.compile(r"^(\s*-\s+)\*?(checkbox)\s")


def _annotate_unchecked_checkboxes(snapshot: str) -> str:
    """Add explicit ``[unchecked]`` to checkboxes without ``[checked]``.

    Playwright marks checked checkboxes with ``[checked]`` but leaves
    unchecked ones unmarked.  In long snapshots (60+ elements), models
    treat "visible" as "selected" because the absence of a tag is a
    weak signal.  Making the unchecked state explicit creates a clear
    "this is NOT selected" signal that persists regardless of snapshot
    length.

    Only targets checkboxes — radio groups already have a clear anchor
    from the one ``[checked]`` item.
    """
    if not snapshot:
        return snapshot
    lines = snapshot.split("\n")
    out: list[str] = []
    for line in lines:
        if _CHECKBOX_LINE_RE.match(line) and "[checked]" not in line:
            # Insert [unchecked] after the last closing quote of the label.
            last_q = line.rfind('"')
            if last_q >= 0:
                out.append(line[: last_q + 1] + " [unchecked]" + line[last_q + 1 :])
                continue
        out.append(line)
    return "\n".join(out)


def _filter_interactive_only(snapshot: str) -> str:
    """Return only interactive elements (lines with @eN refs) as a flat list.

    Like OpenClaw's ``--interactive`` mode: strips the ARIA tree structure
    and returns just the actionable elements.  Useful when the full tree is
    too large or the agent only needs to pick an element to interact with.
    """
    out: list[str] = []
    for line in snapshot.split("\n"):
        if _REF_LINE_RE.search(line):
            stripped = line.lstrip()
            if stripped.startswith("- "):
                stripped = stripped[2:]
            out.append("- %s" % stripped)
    return "\n".join(out)


def _snapshot_fingerprint(snapshot: str | None) -> str:
    """Create a ref-free fingerprint for content comparison.

    Strips ``@eN`` refs (which change every snapshot) so two snapshots of
    the same page content produce identical fingerprints. Also normalizes
    volatile timestamps/counters and removes ad-noise lines so dynamic pages
    still compare as unchanged when only feed chrome moved.
    """
    if not snapshot:
        return ""
    normalized_lines: list[str] = []
    for raw_line in _REF_STRIP_RE.sub("", snapshot).splitlines():
        line = raw_line.strip()
        if not line or _SNAPSHOT_VOLATILE_LINE_RE.search(line):
            continue
        line = _SNAPSHOT_TIME_RE.sub("[time]", line)
        line = _SNAPSHOT_CLOCK_RE.sub("[time]", line)
        line = _SNAPSHOT_DATE_RE.sub("[date]", line)
        line = _SNAPSHOT_COUNTER_RE.sub("[count]", line)
        line = re.sub(r"\s+", " ", line).strip().lower()
        if line:
            normalized_lines.append(line)
    return "\n".join(normalized_lines)


def _fingerprints_equivalent(current_fp: str, previous_fp: str, *, threshold: float = 0.90) -> bool:
    """Return True when two normalized snapshots are effectively the same page."""
    if not current_fp or not previous_fp:
        return False
    if current_fp == previous_fp:
        return True
    similarity = SequenceMatcher(None, current_fp, previous_fp).ratio()
    if similarity >= threshold:
        logger.debug("Snapshot fingerprint fuzzy match %.3f >= %.2f", similarity, threshold)
        return True
    return False


async def _take_ref_snapshot(page: Any) -> str | None:
    """Take an accessibility snapshot with @eN refs, including iframe content.

    Clears the ref map, takes a fresh ARIA snapshot of the main frame, then
    recursively traverses child iframes and injects their content into the
    tree.  Uses ``locator.aria_snapshot()`` which is the stable Playwright
    API (``page.accessibility.snapshot()`` was removed in Playwright 1.49+).

    Refs for iframe elements track their owning Frame so that
    ``resolve_ref()`` clicks/fills within the correct frame context.

    Returns the annotated text, or None if the snapshot fails.
    """
    try:
        if await _browser_payment_observation_refusal("browser_snapshot", page):
            return None
        state_user_id = _get_state_user_id()
        manager.clear_ref_map()
        main_frame = getattr(page, "main_frame", None)
        if main_frame is not None:
            child_frames = getattr(main_frame, "child_frames", None) or []

            # If the page has iframes, give them a moment to load content.
            if child_frames:
                for cf in child_frames[:3]:
                    try:
                        await cf.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        logger.debug("iframe domcontentloaded wait timed out")

            result, _ = await _build_iframe_snapshot(
                main_frame,
                manager,
                ref_counter=0,
                is_main=True,
            )
            if result:
                result = _mark_new_elements(result, user_id=state_user_id)
                result = _annotate_unchecked_checkboxes(result)
                # Sync ref map with visible snapshot refs.
                _visible = set(re.findall(r"e\d+", result))
                manager.restrict_ref_map(_visible)
                return result
            return None
        # Fallback for pages without main_frame (e.g. older Playwright).
        aria_text = await page.locator("body").aria_snapshot()
        if aria_text:
            annotated, _ = _inject_refs_into_aria_snapshot(
                aria_text,
                browser_manager=manager,
            )
            if annotated:
                annotated = _mark_new_elements(annotated, user_id=state_user_id)
                annotated = _annotate_unchecked_checkboxes(annotated)
                _visible = set(re.findall(r"e\d+", annotated))
                manager.restrict_ref_map(_visible)
                return annotated
            return None
    except Exception:
        logger.debug("Accessibility snapshot failed")
    return None


def _has_loading_indicators(snapshot_text: str | None) -> bool:
    """Return True if the snapshot text contains dynamic-loading indicators."""
    if not snapshot_text:
        return False
    lower = snapshot_text.lower()
    return any(p in lower for p in _LOADING_PATTERNS)


async def _wait_and_resnap(page: Any, snapshot_text: str | None) -> str | None:
    """If *snapshot_text* contains loading indicators, wait and re-snapshot.

    Returns the updated snapshot (or the original if waiting didn't help).
    """
    if not _has_loading_indicators(snapshot_text):
        return snapshot_text
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
        await page.wait_for_timeout(1000)
        retry = await _take_ref_snapshot(page)
        if retry:
            return retry
    except Exception:
        logger.debug("Re-snapshot after loading indicator wait failed")
    return snapshot_text


# ===========================================================================
# NAVIGATION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "LAST RESORT for content reading: open a specific web page and return the title, final URL, and accessibility snapshot with @eN refs for interaction. "
        "Use when the page needs JavaScript, login, forms, account/commerce flow, screenshots, or other live interaction, or when web_read cannot extract enough content. "
        "For plain public articles, news, blogs, docs, and fetchable pages found by web_search, web_read is often the lighter reading tool. "
        "When the correct URL is uncertain, use web_search first and inspect the result trust signals before navigating."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_navigate(url: str) -> str:
    """Navigate to a URL and return the page content.

    Use when a task depends on a specific interactive page: filling forms,
    interacting with elements, checking JS-only page details, or completing
    action tasks such as ordering, booking, and filing. For fetchable public
    articles, news, blogs, docs, and other plain pages found by web_search,
    web_read is often enough; navigate when richer page state or interaction
    is needed.

    The response INCLUDES an accessibility snapshot of the page showing all
    interactive elements, headings, links, and forms. You do NOT need to call
    browser_get_links, browser_get_form_fields, or browser_get_text after this
    — the snapshot already contains that information.

    Args:
        url: The URL to navigate to (https:// prefix added if missing).
    """
    if (blocked := _browser_disabled()) is not None:
        return blocked

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

        # Validate scheme and check for SSRF before navigating.
        if url != "about:blank":
            url_error = _validate_url(url)
            if url_error:
                payload = {"ok": False, "success": False, "error": url_error}
                payload.update(_navigation_fact_fields(url, error_text=url_error))
                return _json(payload)

        result = await manager.navigate(url)
        status = result.get("http_status_code") or result.get("status")
        redirect_chain = result.get("redirect_chain") if isinstance(result.get("redirect_chain"), list) else []
        result.update(
            _navigation_fact_fields(
                result.get("url") or url,
                http_status_code=status if isinstance(status, int) else None,
                redirect_chain=redirect_chain,
            )
        )

        if error_page := _http_error_page_payload(
            title=result.get("title"),
            url=result.get("url"),
            description=result.get("description"),
            status=result.get("status"),
        ):
            result.update(error_page)

        # M9: Post-navigation SSRF check — catch DNS rebinding attacks
        # where pre-flight DNS returned a public IP but Playwright resolved
        # to a private IP on its second, independent DNS lookup.
        _rebind_page = await manager.get_page()
        _rebind_err = await _post_navigate_ssrf_check(_rebind_page, _PRIVATE_NETWORKS)
        if _rebind_err:
            return _json({"ok": False, "success": False, "error": _rebind_err})

        # Auto-include accessibility snapshot with @eN refs.
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            if snapshot_text:
                result["snapshot"] = snapshot_text
        except Exception:
            logger.debug("Snapshot after navigate failed, continuing without it")

        # Structured signal for the agent loop: a successful navigate ALWAYS
        # invalidates any @eN refs from prior snapshots — the new page has its
        # own snapshot below. This is the producer-side replacement for the
        # legacy `"navigated": true in tool_result_str` substring scan in
        # intent/agent_loop.py (see R5-P0-O ratchet).
        result["refs_invalidated"] = True
        result["ref_invalidation_reason"] = "navigate"

        # Dead / parked page detection records objective page-health facts.
        page_health = _check_dead_page(
            result.get("title"),
            result.get("description"),
            result.get("snapshot"),
        )
        if page_health:
            result["page_health"] = page_health

        # Same-URL detection — when model navigates to a URL it just
        # visited, record that the page is likely unchanged.
        # Evidence: task dd1f9ae44329 navigated to eforms.com/llc/wi/ 5×,
        # task f42d87b75bfb navigated to bestbuy store-locator 3×.
        _norm_url = url.rstrip("/").lower()
        page = await manager.get_page()
        state = _get_hint_state(page)
        if _norm_url and _norm_url == state.last_navigate_url:
            result["same_url_as_previous_navigation"] = True
            result["previous_navigation_url"] = state.last_navigate_url
        state.last_navigate_url = _norm_url
        state.last_snapshot_fingerprint = _snapshot_fingerprint(result.get("snapshot", "") or "")
        _reset_no_effect_state(state)

        await _enrich_auto_screenshot(page, result)
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
    description=("Go back one page in browser history and return the previous page's title and URL."),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_back() -> str:
    """Go back one page in the browser history."""
    try:
        page = await manager.get_page()
        await page.go_back(timeout=30000, wait_until="domcontentloaded")
        title = await page.title()
        # browser_back ALWAYS invalidates any @eN refs from prior snapshots.
        return _json(
            {
                "title": title,
                "url": page.url,
                "refs_invalidated": True,
                "ref_invalidation_reason": "back",
            }
        )
    except Exception as exc:
        return _json({"error": "Go back failed: %s" % exc})


@server.tool(
    description=(
        "Go forward one page in browser history, reversing a previous browser_back, and return the page's title and URL."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_forward() -> str:
    """Go forward one page in the browser history."""
    try:
        page = await manager.get_page()
        await page.go_forward(timeout=30000, wait_until="domcontentloaded")
        title = await page.title()
        # browser_forward ALWAYS invalidates any @eN refs from prior snapshots.
        return _json(
            {
                "title": title,
                "url": page.url,
                "refs_invalidated": True,
                "ref_invalidation_reason": "forward",
            }
        )
    except Exception as exc:
        return _json({"error": "Go forward failed: %s" % exc})


@server.tool(
    description=("Reload the current page and return its title and URL."),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_refresh() -> str:
    """Reload the current page."""
    try:
        page = await manager.get_page()
        await page.reload(timeout=30000, wait_until="domcontentloaded")
        title = await page.title()
        # A reload invalidates any @eN refs from the previous render.
        return _json(
            {
                "title": title,
                "url": page.url,
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
        "Extract raw innerText from the full page body or a specific element by CSS selector or @eN ref, with offset/max_chars chunking."
        + _SELECTOR_SYNTAX_DOC
        + _REF_USAGE_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_get_text(selector: str = "", max_chars: int | None = None, offset: int = 0) -> str:
    """Get text content from the page or a specific element.

    Usually not needed — browser_navigate and browser_click responses include
    accessibility snapshots with page text. Use this only when you need the
    raw text of a specific element that the snapshot doesn't cover.

    Args:
        selector: CSS selector or @eN ref from a snapshot. Descriptive selectors
            ending in @eN are also supported (empty = full page body text,
            chunked by the configured browser_get_text limits).
        max_chars: Maximum characters to return.
        offset: Character offset for continuing long page text.
    """
    if block := _payment_gate_observation_refusal("browser_get_text"):
        return block
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_get_text", page):
            return block
        if selector and (bogus := _bogus_selector_error(selector)) is not None:
            return bogus
        # @ref auto-detection
        _ref_match = re.match(r"^(?:.*\s)?@@?(e\d+)$", selector.strip())
        if _ref_match:
            ref_id = _ref_match.group(1)
            try:
                locator = manager.resolve_ref(ref_id)
                text_content = await locator.first.inner_text(timeout=5000)
                payload = _text_slice_payload(text_content, offset=offset, max_chars=max_chars)
                payload["selector"] = ref_id
                return _json(payload)
            except Exception as exc:
                payload = {
                    "ok": False,
                    "error": "Failed to get text for ref '%s': %s" % (ref_id, str(exc)[:200]),
                }
                payload.update(_navigation_fact_fields(page.url, error_text=str(exc)))
                return _json(payload)

        target = selector.strip() if selector else "body"
        locator = page.locator(target)
        match_count = await locator.count()
        if match_count == 0:
            payload = {
                "ok": False,
                "error": "Selector matched 0 elements: %s" % target,
                "url": page.url,
                "selector": target,
                "match_count": 0,
            }
            payload.update(_navigation_fact_fields(page.url))
            return _json(payload)
        text = await locator.first.inner_text(timeout=10000)
        payload = _text_slice_payload(text, offset=offset, max_chars=max_chars)
        payload["url"] = page.url
        payload["selector"] = target
        payload["match_count"] = match_count
        return _json(payload)
    except _BROWSER_OPERATION_ERRORS as exc:
        current_url = ""
        try:
            current_url = (await manager.get_page()).url
        except _BROWSER_OPERATION_ERRORS:
            logger.debug("Could not read current browser URL for browser_get_text error")
        payload = {
            "ok": False,
            "error": "Failed to get text for '%s': %s" % (selector, exc),
        }
        payload.update(_navigation_fact_fields(current_url, error_text=str(exc)))
        return _json(payload)


@server.tool(
    description=(
        "Get all anchor links on the page as {text, href} pairs with full URLs, optionally scoped to a CSS selector."
        + _SELECTOR_SYNTAX_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_get_links(selector: str = "") -> str:
    """Get all links on the page as a JSON array of {text, href}.

    Usually not needed — browser_navigate includes an accessibility snapshot
    that shows all links with their text and roles. Use this only when you
    need full href URLs that the snapshot doesn't include.

    Args:
        selector: Optional CSS selector to scope the search (empty = whole page).
    """
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_get_links", page):
            return block
        scope = selector.strip() if selector else "body"
        links = await page.evaluate(
            """(scope) => {
                const root = scope === 'body'
                    ? document.body
                    : document.querySelector(scope) || document.body;
                return Array.from(root.querySelectorAll('a[href]')).slice(0, 50).map((a, i) => ({
                    index: i,
                    text: (a.innerText || '').trim().substring(0, 120),
                    href: a.href,
                }));
            }""",
            scope,
        )
        return _json({"url": page.url, "links": links, "count": len(links)})
    except Exception as exc:
        return _json({"error": "Failed to get links: %s" % exc})


@server.tool(
    description=(
        "Get all visible form fields and submit buttons with their CSS selectors, excluding hidden and reCAPTCHA inputs."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_form_fields() -> str:
    """Get all visible form input fields and submit buttons with CSS selectors.

    Usually not needed — browser_navigate includes an accessibility snapshot
    that shows form fields. Use this only when you need exact CSS selectors
    for fields that the snapshot doesn't clearly identify.

    Returns JSON with fields array and buttons array. Hidden inputs and
    recaptcha fields are excluded to reduce noise.
    """
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_get_form_fields", page):
            return block
        data = await page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll(
                    'input, textarea, select'
                )).filter(el => {
                    // Skip hidden, recaptcha, and invisible elements.
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
                    // Walk up to find a label wrapper if no for= label found.
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
                // Find submit-like buttons near forms.
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
                // Also grab any prominent buttons with action-oriented text.
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
                // Deduplicate by text.
                const seen = new Set();
                const uniqueBtns = allBtns.filter(b => {
                    if (seen.has(b.text)) return false;
                    seen.add(b.text);
                    return true;
                });
                return { fields, buttons: uniqueBtns };
            }""")
        return _json(
            {
                "url": page.url,
                "fields": data["fields"],
                "count": len(data["fields"]),
                "submit_buttons": data["buttons"],
            }
        )
    except Exception as exc:
        return _json({"error": "Failed to get form fields: %s" % exc})


@server.tool(
    description=(
        "Get a lightweight page overview with URL, title, meta description, buttons, inputs, and links -- no @eN refs."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_page_info() -> str:
    """Get current URL, title, meta description, and main interactive elements."""
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_get_page_info", page):
            return block
        info = await page.evaluate("""() => {
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
            }""")
        return _json(info)
    except Exception as exc:
        return _json({"error": "Failed to get page info: %s" % exc})


@server.tool(
    description=(
        "Get the page's accessibility tree with @eN refs for clicking, filling, and selecting elements."
        + _REF_USAGE_DOC
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def browser_snapshot(
    wait_for_stable: bool = False,
    mode: str = "full",
) -> str:
    """Get fresh page state with @eN refs for click_ref/fill_ref/select_ref. Cheaper than screenshot.

    Call this after interactions that change page content to get updated refs.
    Read element labels carefully before choosing refs — label, not position,
    determines the correct ref. Always call this after a failed interaction
    to get the current page state before trying again.

    Newly-appeared elements (not in the previous snapshot) are marked with
    a ``*`` prefix so you can quickly see what changed.

    Args:
        wait_for_stable: Wait for network to settle before snapshotting (default false).
        mode: ``"full"`` (default) returns the complete ARIA tree.
              ``"interactive"`` returns only actionable elements as a flat
              list — useful when the page is large and you just need to
              pick an element.
    """
    if block := _payment_gate_observation_refusal("browser_snapshot"):
        return block
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_snapshot", page):
            return block

        if wait_for_stable:
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                logger.debug("networkidle wait timed out before snapshot")

        snapshot_text = await _take_ref_snapshot(page)
        if not snapshot_text:
            return _json({"error": "No accessibility snapshot available"})

        # Auto-detect loading indicators and re-snapshot if found.
        snapshot_text = await _wait_and_resnap(page, snapshot_text) or snapshot_text

        # Interactive-only mode: flat list of actionable elements.
        display_text = snapshot_text
        if mode == "interactive":
            display_text = _filter_interactive_only(snapshot_text)
        snapshot_original_chars = len(display_text)
        snapshot_truncated = snapshot_original_chars > _BROWSER_SNAPSHOT_MAX_LLM_CHARS
        if snapshot_truncated:
            display_text = _truncate(display_text, _BROWSER_SNAPSHOT_MAX_LLM_CHARS)

        title = await page.title()
        result: dict[str, Any] = {
            "snapshot": display_text,
            "url": page.url,
            "title": title,
            "snapshot_truncated": snapshot_truncated,
            "snapshot_chars_original": snapshot_original_chars,
        }

        page_health = _check_dead_page(title, None, snapshot_text)
        if page_health:
            result["page_health"] = page_health

        page = await manager.get_page()
        state = _get_hint_state(page)
        current_fp = _snapshot_fingerprint(snapshot_text)
        if _fingerprints_equivalent(current_fp, state.last_snapshot_fingerprint):
            result["snapshot_unchanged_from_previous"] = True
        state.last_snapshot_fingerprint = current_fp

        fill_form_buttons = _fill_form_button_candidates(snapshot_text)
        if fill_form_buttons and "textbox" in snapshot_text:
            result["fill_form_button_candidates"] = fill_form_buttons

        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except Exception as exc:
        return _json({"error": "Snapshot failed: %s" % exc})


# ===========================================================================
# INTERACTION TOOLS
# ===========================================================================


async def _click_candidates(page: Any, search_text: str) -> list[dict[str, Any]]:
    """Return visible clickable candidates that fuzzily resemble *search_text*."""

    try:
        candidates = await page.evaluate(
            """(searchText) => {
                const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                const query = norm(searchText);
                if (!query) return [];
                const words = query.split(' ').filter(Boolean);
                const els = Array.from(document.querySelectorAll(
                    'a, button, input[type="submit"], input[type="button"], [role="button"], [role="link"]'
                ));
                return els.map((el) => {
                    const rect = el.getBoundingClientRect();
                    if (!rect || rect.width <= 0 || rect.height <= 0) return null;
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
            }""",
            search_text,
        )
    except (RuntimeError, TypeError, ValueError):
        return []
    return candidates if isinstance(candidates, list) else []


async def _do_click(selector: str, text: str = "") -> str:
    """Click an element by CSS selector or text. Internal handler for browser_interact(action='click')."""
    # Fast-fail on jQuery-style pseudo-selectors before we burn 12s on
    # Playwright strategies that will all miss.
    if (bogus := _bogus_selector_error(selector)) is not None:
        return bogus
    popup_cleanup = None
    try:
        # Strategy 0: @ref auto-detection - redirect to ref-based clicking.
        # The model sometimes passes @eN refs to browser_click instead of browser_interact(action='click_ref').
        _ref_match = re.match(r"^(?:.*\s)?@@?(e\d+)$", selector.strip())
        if _ref_match:
            ref_id = _ref_match.group(1)
            try:
                locator = manager.resolve_ref(ref_id)
                if await locator.count() > 0:
                    return await _do_click_ref(ref_id)
            except Exception:
                logger.debug(
                    "@ref auto-detection failed for %s, falling through to CSS/text strategies",
                    selector,
                )

        page = await manager.get_page()
        popup_future, popup_cleanup, popup_prev_url = manager.begin_popup_watch()
        clicked = False
        clicked_text = ""
        match_count = 0
        submit_control_click = False
        submit_click_meta: dict[str, Any] = {}
        clicked_href = ""
        click_strategy = ""

        # Snapshot page state before click for change detection.
        url_before = page.url
        fingerprint_before = await _playwright_page_fingerprint(page)
        field_count_before = 0
        try:
            field_count_before = await page.evaluate(
                "() => document.querySelectorAll('input, textarea, select').length"
            )
        except Exception:
            logger.debug("Could not count form fields before click")

        # When text is provided, use it as the search string for text/role strategies.
        search_text = text if text else selector
        _CLICK_MS = 3000  # per-strategy timeout

        # ── Payment Gate (deterministic) ─────────────────────────────
        # Check selector text, explicit text arg, and search_text for
        # payment-method or order-submission triggers.
        for _pg_text in {selector, text, search_text}:
            _pay_block = await _is_payment_blocked(_pg_text)
            if _pay_block:
                logger.warning(
                    "Payment gate blocked click (selector=%r, text=%r)",
                    selector[:60],
                    text[:60],
                )
                popup_cleanup()
                popup_cleanup = None
                return _json({"ok": False, "error": _pay_block})

        async def _attempt_locator_click(loc: Any, strategy_name: str) -> bool:
            nonlocal clicked_href, clicked_text, click_strategy, match_count
            nonlocal popup_cleanup, submit_click_meta, submit_control_click

            count = await loc.count()
            if count <= 0:
                return False
            match_count = count
            _pay_block, _blocked_signal = await _payment_gate_block_from_locator(
                loc,
                selector,
                text,
                search_text,
            )
            if _pay_block:
                logger.warning(
                    "Payment gate blocked %s click (search_text=%r, signal=%r)",
                    strategy_name,
                    search_text[:60],
                    _blocked_signal[:80],
                )
                popup_cleanup()
                popup_cleanup = None
                raise PermissionError(_pay_block)
            first = loc.first
            try:
                clicked_text = (await first.inner_text())[:80]
            except _BROWSER_OPERATION_ERRORS:
                logger.debug("Could not read inner_text for %s element", strategy_name)
            try:
                href = await first.evaluate(
                    """el => {
                        const anchor = el && el.closest ? el.closest('a[href]') : null;
                        return anchor ? anchor.href : '';
                    }""",
                    timeout=500,
                )
                if isinstance(href, str) and href.strip():
                    clicked_href = href.strip()
            except _BROWSER_OPERATION_ERRORS:
                logger.debug("Could not read href for %s element", strategy_name)
            submit_control_click = await _locator_is_submit_control(loc)
            if submit_control_click:
                submit_click_meta = await _click_submit_with_fallback(
                    loc,
                    page,
                    url_before,
                    fingerprint_before,
                    timeout=_CLICK_MS,
                )
            else:
                await first.click(timeout=_CLICK_MS)
            click_strategy = strategy_name
            return True

        if _is_css_selector(selector):
            try:
                loc = page.locator(selector)
                if text:
                    loc = loc.filter(has_text=text)
                clicked = await _attempt_locator_click(loc, "css")
            except PermissionError as exc:
                return _json({"ok": False, "error": str(exc)})
            except Exception:
                logger.debug("CSS selector click strategy failed for %s", selector)

        for role_name in ("link", "button"):
            if clicked:
                break
            for exact in (True, False):
                try:
                    loc = page.get_by_role(role_name, name=search_text, exact=exact)
                    clicked = await _attempt_locator_click(loc, "role-%s" % role_name)
                    if clicked:
                        break
                except PermissionError as exc:
                    return _json({"ok": False, "error": str(exc)})
                except _BROWSER_OPERATION_ERRORS:
                    logger.debug("%s role click strategy failed for %s", role_name, search_text)

        if not clicked:
            try:
                loc = page.get_by_text(search_text, exact=False)
                clicked = await _attempt_locator_click(loc, "text")
            except PermissionError as exc:
                return _json({"ok": False, "error": str(exc)})
            except Exception:
                logger.debug("Text-match click strategy failed for %s", search_text)

        if not clicked:
            popup_cleanup()
            popup_cleanup = None
            candidates = await _click_candidates(page, search_text)
            return _json(
                {
                    "ok": False,
                    "error": "Could not find clickable element for '%s'." % selector,
                    **({"candidates": candidates} if candidates else {}),
                }
            )

        # Check if click opened a new tab (target="_blank", window.open, etc.).
        popup_result = await manager.resolve_popup_watch(
            popup_future,
            popup_cleanup,
            popup_prev_url,
        )
        popup_cleanup = None  # Cleanup done by resolve_popup_watch.
        page = await manager.get_page()  # May have changed if new tab opened.

        if not popup_result["new_tab"] and not submit_control_click:
            # Wait for possible navigation / SPA content load.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                logger.debug("domcontentloaded wait timed out after click")
            # Brief wait for SPA frameworks to render after click.
            try:
                await page.wait_for_timeout(500)
            except Exception:
                logger.debug("Post-click render wait failed")

        href_followed = False
        href_follow_error = ""
        if not popup_result["new_tab"] and not submit_control_click and clicked_href and page.url == url_before:
            href_url = urljoin(page.url, clicked_href)
            if href_url != url_before:
                href_follow_error = _validate_url(href_url) or ""
                if not href_follow_error:
                    try:
                        await page.goto(href_url, wait_until="domcontentloaded", timeout=15000)
                        href_followed = True
                    except _BROWSER_OPERATION_ERRORS as exc:
                        href_follow_error = str(exc)

        title = await page.title()
        result: dict[str, Any] = {
            "clicked": clicked_text or selector,
            "title": title,
            "url": page.url,
        }
        if click_strategy:
            result["click_strategy"] = click_strategy
        if clicked_href:
            result["clicked_href"] = clicked_href
        if href_followed:
            result["href_followed"] = True
        elif href_follow_error:
            result["href_follow_error"] = href_follow_error
        if submit_click_meta:
            result.update(submit_click_meta)
        if popup_result.get("new_tab"):
            result["new_tab"] = True
            result["previous_url"] = popup_result["previous_url"]

        # Detect page changes so the agent knows if the click had an effect.
        if page.url != url_before:
            result["navigated"] = True
            # Structured signal for the agent loop — selector-click that caused
            # a same-tab navigation must also invalidate stale @eN refs (parity
            # with the ref-click path above).
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "click_navigated"
        field_count_after = field_count_before
        try:
            field_count_after = await page.evaluate("() => document.querySelectorAll('input, textarea, select').length")
            if field_count_after != field_count_before:
                result["new_fields"] = field_count_after - field_count_before
                result["total_fields"] = field_count_after
        except Exception:
            logger.debug("Could not count form fields after click")

        # If form was submitted (fields disappeared) or new page loaded with
        # many new fields, wait for network to settle so the next page renders.
        field_delta = field_count_after - field_count_before
        if abs(field_delta) >= 3 or page.url != url_before:
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                logger.debug("networkidle wait timed out after click navigation")
            # Re-capture page state after transition.
            result["title"] = await page.title()
            result["url"] = page.url
            if page.url != url_before:
                result["navigated"] = True
                result["refs_invalidated"] = True
                result["ref_invalidation_reason"] = "click_navigated"
        if match_count > 1:
            result["match_count"] = match_count

        # Auto-include accessibility snapshot with @eN refs after click.
        try:
            snapshot_text = await _take_ref_snapshot(page)
            # Detect loading indicators and re-snapshot if content is still loading.
            snapshot_text = await _wait_and_resnap(page, snapshot_text)
            if snapshot_text:
                result["snapshot"] = snapshot_text
        except Exception:
            logger.debug("Snapshot after click failed, continuing without it")

        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except Exception as exc:
        if popup_cleanup:
            popup_cleanup()
        return _json({"error": "Click failed: %s" % exc})


async def _do_type(selector: str, text: str, clear_first: bool = True) -> str:
    """Type text into an input field. Internal handler for browser_interact(action='type')."""
    # Fast-fail on jQuery-style pseudo-selectors so the agent gets a
    # snapshot+ref hint in <50ms instead of waiting for Playwright timeouts.
    # @eN refs (e.g. "@e5") are explicitly handled below and are not bogus.
    if not re.match(r"^@?e\d+$", selector.strip()):
        if (bogus := _bogus_selector_error(selector)) is not None:
            return bogus
    # ── Payment Gate: reject credit card numbers ──────────────────────
    if (violation := _payment_value_violation(text)) is not None:
        logger.warning(
            "Payment gate blocked type (sensitive payment value, selector=%r)",
            selector[:60],
        )
        return _json({"ok": False, "error": violation})

    try:
        page = await manager.get_page()
        # Resolve @eN refs — the model frequently passes refs instead of CSS selectors
        if re.match(r"^@?e\d+$", selector):
            locator = manager.resolve_ref(selector)
            _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, selector)
            if _field_block:
                return _json({"ok": False, "error": _field_block})
            tag = ""
            input_type = ""
            try:
                tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
                if tag == "input":
                    input_type = await locator.first.evaluate("el => (el.type || '').toLowerCase()", timeout=2000)
            except _BROWSER_OPERATION_ERRORS:
                logger.debug("Type element metadata lookup failed")
            if clear_first:
                if _is_text_value_verification_target(tag, input_type):
                    await _fill_text_locator_verified(locator, text, timeout=10000)
                else:
                    await locator.fill(text, timeout=10000)
            else:
                await locator.press_sequentially(text, timeout=10000)
                if _is_text_value_verification_target(tag, input_type):
                    verified, current = await _verify_locator_text_value(locator, text)
                    if not verified:
                        raise RuntimeError("Typed value did not persist; current value is %r" % current)
        elif clear_first:
            _loc = page.locator(selector)
            _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, selector)
            if _field_block:
                return _json({"ok": False, "error": _field_block})
            await page.fill(selector, text, timeout=10000)
        else:
            _loc = page.locator(selector)
            _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, selector)
            if _field_block:
                return _json({"ok": False, "error": _field_block})
            await page.locator(selector).press_sequentially(text, timeout=10000)
        return _json({"typed": text, "selector": selector, "cleared": clear_first})
    except ValueError as ref_exc:
        return _json({"error": str(ref_exc)})
    except Exception as exc:
        return _json({"error": "Type failed for '%s': %s" % (selector, exc)})


async def _do_select(selector: str, value: str) -> str:
    """Select a dropdown option by CSS selector. Internal handler for browser_interact(action='select')."""
    # Fast-fail on jQuery-style pseudo-selectors.  @eN refs are valid and
    # handled below; only flag truly malformed selectors here.
    if not re.match(r"^@?e\d+$", selector.strip()):
        if (bogus := _bogus_selector_error(selector)) is not None:
            return bogus
    # ── Payment Gate (deterministic) ─────────────────────────────────
    _pay_block = await _is_payment_blocked(value)
    if _pay_block:
        logger.warning(
            "Payment gate blocked select (selector=%r, value=%r)",
            selector[:60],
            value[:60],
        )
        return _json({"ok": False, "error": _pay_block})

    try:
        page = await manager.get_page()
        # Resolve @eN refs — the model frequently passes refs instead of CSS selectors
        if re.match(r"^@?e\d+$", selector):
            locator = manager.resolve_ref(selector)
            _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, selector)
            if _field_block:
                return _json({"ok": False, "error": _field_block})
            _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, selector, value)
            if _pay_block:
                return _json({"ok": False, "error": _pay_block})
            await locator.select_option(value, timeout=10000)
        else:
            _loc = page.locator(selector)
            _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, selector)
            if _field_block:
                return _json({"ok": False, "error": _field_block})
            _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, selector, value)
            if _pay_block:
                return _json({"ok": False, "error": _pay_block})
            await page.select_option(selector, value, timeout=10000)
        return _json({"selected": value, "selector": selector})
    except ValueError as ref_exc:
        return _json({"error": str(ref_exc)})
    except Exception as exc:
        return _json({"error": "Select failed for '%s': %s" % (selector, exc)})


@server.tool(
    description=("Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.) on the currently focused element."),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def browser_press_key(key: str) -> str:
    """Press a keyboard key (Enter, Tab, Escape, etc.).

    Args:
        key: Key name (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').
    """
    try:
        page = await manager.get_page()

        # ── Payment Gate: check focused element for Enter/Return/Space ──
        # The model can Tab to a "Place Order" button and press Enter to
        # bypass the click-level payment gate.  Inspect the active element.
        if key.lower() in ("enter", "return", " ", "space"):
            try:
                _focused_info = await page.evaluate("""() => {
                        const el = document.activeElement;
                        if (!el || el === document.body) return null;
                        const tag = el.tagName.toLowerCase();
                        // Only gate interactive elements (buttons, links, inputs)
                        if (!['button', 'a', 'input', 'summary'].includes(tag)
                            && !el.getAttribute('role')) return null;
                        return {
                            tag: tag,
                            text: (el.innerText || '').substring(0, 200),
                            value: ('value' in el && el.value != null) ? String(el.value).substring(0, 200) : '',
                            ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 200),
                            nameAttr: (el.getAttribute('name') || '').substring(0, 200),
                            title: (el.getAttribute('title') || '').substring(0, 200),
                            type: el.type || '',
                            role: el.getAttribute('role') || '',
                            name: (
                                el.getAttribute('aria-label')
                                || el.getAttribute('name')
                                || el.getAttribute('title')
                                || el.innerText
                                || el.textContent
                                || ''
                            ).substring(0, 200)
                        };
                    }""")
                if _focused_info:
                    _focused_candidates = _payment_probe_strings_from_element_info(_focused_info)
                    _pay_block = None
                    _blocked_candidate = ""
                    for _candidate in _focused_candidates:
                        _pay_block = await _is_payment_blocked(_candidate)
                        if _pay_block and not _is_safe_pattern(_candidate):
                            _blocked_candidate = _candidate
                            break
                    if _pay_block:
                        logger.warning(
                            "Payment gate blocked key press '%s' on focused element (signal=%r)",
                            key,
                            _blocked_candidate[:80],
                        )
                        return _json({"ok": False, "error": _pay_block})
            except Exception:
                # If we can't inspect the focused element, allow the key press
                # (don't break keyboard navigation for edge cases).
                logger.debug("Could not inspect focused element for payment gate check")

        url_before = page.url
        await page.keyboard.press(key)
        # If the key could trigger navigation (Enter, etc.), wait briefly
        if key.lower() in ("enter", "return"):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                logger.debug("domcontentloaded wait timed out after key press")
        result: dict = {"pressed": key}
        if page.url != url_before:
            result["navigated_to"] = page.url
            # Same-tab navigation from a key press invalidates @eN refs.
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "key_navigated"
        return _json(result)
    except Exception as exc:
        return _json({"error": "Key press failed for '%s': %s" % (key, exc)})


@server.tool(
    description=("Scroll the page up or down by a number of viewport-heights to reveal off-screen content."),
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
        page = await manager.get_page()
        delta = amount * 400
        if direction.lower() == "up":
            delta = -delta
        await page.mouse.wheel(0, delta)
        # Small wait for content to load after scroll
        await page.wait_for_timeout(500)
        scroll_pos = await page.evaluate("() => window.scrollY")
        return _json({"direction": direction, "amount": amount, "scroll_y": scroll_pos})
    except Exception as exc:
        return _json({"error": "Scroll failed: %s" % exc})


# ===========================================================================
# PAYMENT GATE — deterministic code-level enforcement
# ===========================================================================

# Per-session payment gate override registry.
#
# P0 FIX (2026-04-10): The old implementation used a MODULE-LEVEL GLOBAL bool.
# In a multi-user SaaS, User A approving their payment would allow User B's
# agent to bypass the gate.  Now overrides are scoped to a session_id, are
# consumed after ONE use, and expire after _OVERRIDE_TTL_SECONDS.
#
# The current session is communicated via a ``contextvars.ContextVar`` so that
# the 10+ internal call-sites (``_is_payment_blocked``, ``_is_js_payment_action``,
# ``_do_click_ref``, etc.) do NOT need to thread a session_id parameter.
# The executor sets the context var before each tool dispatch.

# Context vars: set by the executor (or MCP call_tool wrapper) before each
# browser tool call so the payment/signature gates know which session is active.
_current_payment_session: contextvars.ContextVar[str] = contextvars.ContextVar("_current_payment_session", default="")
_current_signature_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_signature_session",
    default="",
)

_OVERRIDE_TTL_SECONDS: float = 60.0  # override expires after 60 seconds
_OVERRIDE_STALE_SECONDS: float = TIMEOUT_5_MINUTES
_SIGNATURE_GATE_OVERRIDE_ACTIONS = 4

# Registry: {session_id: monotonic_timestamp_when_granted}
# Protected by a lock for thread-safety even though the primary path is
# single-event-loop async (defense in depth for future multi-worker deploys).
_payment_gate_overrides: dict[str, float] = {}
_payment_gate_override_tokens: dict[str, float] = {}
_payment_gate_lock = threading.Lock()
_signature_gate_overrides: dict[str, tuple[float, int]] = {}
_signature_gate_override_tokens: dict[str, float] = {}
_signature_gate_lock = threading.Lock()


def _prune_payment_gate_overrides_locked(now_monotonic: float) -> tuple[int, int]:
    """Remove stale entries and enforce the maximum registry size.

    Callers must hold ``_payment_gate_lock``.
    """
    expired_sessions = [
        session_id
        for session_id, granted_at in _payment_gate_overrides.items()
        if now_monotonic - granted_at > _OVERRIDE_STALE_SECONDS
    ]
    for session_id in expired_sessions:
        _payment_gate_overrides.pop(session_id, None)
    expired_tokens = [
        token
        for token, granted_at in _payment_gate_override_tokens.items()
        if now_monotonic - granted_at > _OVERRIDE_STALE_SECONDS
    ]
    for token in expired_tokens:
        _payment_gate_override_tokens.pop(token, None)

    evicted = 0
    if len(_payment_gate_overrides) >= PAYMENT_GATE_MAX_OVERRIDES:
        overflow = len(_payment_gate_overrides) - PAYMENT_GATE_MAX_OVERRIDES + 1
        oldest_sessions = sorted(_payment_gate_overrides.items(), key=lambda item: item[1])[:overflow]
        for session_id, _granted_at in oldest_sessions:
            _payment_gate_overrides.pop(session_id, None)
        evicted = len(oldest_sessions)

    return len(expired_sessions) + len(expired_tokens), evicted


def set_payment_session(session_id: str) -> contextvars.Token[str]:
    """Set the active payment session for the current async context.

    Returns a token that can be passed to ``reset_payment_session()`` to
    restore the previous value.  Typically used as::

        tok = set_payment_session(self._session_id or self.task_id)
        try:
            await call_browser_tool(...)
        finally:
            reset_payment_session(tok)
    """
    return _current_payment_session.set(session_id)


def reset_payment_session(token: contextvars.Token[str]) -> None:
    """Restore the previous payment session context."""
    _current_payment_session.reset(token)


def _prune_signature_gate_overrides_locked(now_monotonic: float) -> tuple[int, int]:
    """Remove stale signature entries and enforce the maximum registry size."""
    expired_sessions = [
        session_id
        for session_id, (granted_at, _remaining) in _signature_gate_overrides.items()
        if now_monotonic - granted_at > _OVERRIDE_STALE_SECONDS
    ]
    for session_id in expired_sessions:
        _signature_gate_overrides.pop(session_id, None)
    expired_tokens = [
        token
        for token, seen_at in _signature_gate_override_tokens.items()
        if now_monotonic - seen_at > _OVERRIDE_STALE_SECONDS
    ]
    for token in expired_tokens:
        _signature_gate_override_tokens.pop(token, None)

    evicted = 0
    if len(_signature_gate_overrides) >= PAYMENT_GATE_MAX_OVERRIDES:
        overflow = len(_signature_gate_overrides) - PAYMENT_GATE_MAX_OVERRIDES + 1
        oldest_sessions = sorted(_signature_gate_overrides.items(), key=lambda item: item[1][0])[:overflow]
        for session_id, _entry in oldest_sessions:
            _signature_gate_overrides.pop(session_id, None)
        evicted = len(oldest_sessions)

    return len(expired_sessions) + len(expired_tokens), evicted


def set_signature_session(session_id: str) -> contextvars.Token[str]:
    """Set the active signature session for the current async context."""
    return _current_signature_session.set(session_id)


def reset_signature_session(token: contextvars.Token[str]) -> None:
    """Restore the previous signature session context."""
    _current_signature_session.reset(token)


def grant_payment_gate_override(session_id: str) -> None:
    """Allow exactly ONE subsequent payment-gated action for *session_id*.

    Called by ``AgentExecutor._start_payment_confirmation`` after the user
    confirms the order.  The override is consumed by the first call to
    ``_is_payment_blocked()`` or ``_is_js_payment_action()`` that would
    otherwise block — but ONLY if the consuming call's session matches.

    The override expires after ``_OVERRIDE_TTL_SECONDS`` (60 s).
    """
    if not session_id:
        logger.error("grant_payment_gate_override called with empty session_id — ignoring")
        return
    now_monotonic = time.monotonic()
    with _payment_gate_lock:
        expired_count, evicted_count = _prune_payment_gate_overrides_locked(now_monotonic)
        _payment_gate_overrides[session_id] = now_monotonic
    if expired_count:
        logger.debug("Payment gate override cleanup removed %d stale entries", expired_count)
    if evicted_count:
        logger.warning(
            "Payment gate override registry hit cap=%d; evicted %d oldest entries",
            PAYMENT_GATE_MAX_OVERRIDES,
            evicted_count,
        )
    logger.info(
        "Payment gate override GRANTED for session=%s (TTL=%ds)",
        session_id,
        int(_OVERRIDE_TTL_SECONDS),
    )


def grant_signature_gate_override(session_id: str, *, actions: int = _SIGNATURE_GATE_OVERRIDE_ACTIONS) -> None:
    """Allow a short bounded sequence of signature-gated actions for *session_id*."""
    if not session_id:
        logger.error("grant_signature_gate_override called with empty session_id — ignoring")
        return
    now_monotonic = time.monotonic()
    remaining_actions = max(1, int(actions))
    with _signature_gate_lock:
        expired_count, evicted_count = _prune_signature_gate_overrides_locked(now_monotonic)
        _signature_gate_overrides[session_id] = (now_monotonic, remaining_actions)
    if expired_count:
        logger.debug("Signature gate override cleanup removed %d stale entries", expired_count)
    if evicted_count:
        logger.warning(
            "Signature gate override registry hit cap=%d; evicted %d oldest entries",
            PAYMENT_GATE_MAX_OVERRIDES,
            evicted_count,
        )
    logger.info(
        "Signature gate override GRANTED for session=%s (TTL=%ds, actions=%d)",
        session_id,
        int(_OVERRIDE_TTL_SECONDS),
        remaining_actions,
    )


def revoke_payment_gate_override(session_id: str) -> None:
    """Revoke the override for *session_id* without consuming it (e.g., on task cleanup)."""
    if not session_id:
        return
    now_monotonic = time.monotonic()
    with _payment_gate_lock:
        expired_count, _evicted_count = _prune_payment_gate_overrides_locked(now_monotonic)
        removed = _payment_gate_overrides.pop(session_id, None)
    if expired_count:
        logger.debug("Payment gate override cleanup removed %d stale entries", expired_count)
    if removed is not None:
        logger.info("Payment gate override REVOKED for session=%s (unconsumed)", session_id)


def revoke_signature_gate_override(session_id: str) -> None:
    """Revoke the signature override for *session_id* without consuming it."""
    if not session_id:
        return
    now_monotonic = time.monotonic()
    with _signature_gate_lock:
        expired_count, _evicted_count = _prune_signature_gate_overrides_locked(now_monotonic)
        removed = _signature_gate_overrides.pop(session_id, None)
    if expired_count:
        logger.debug("Signature gate override cleanup removed %d stale entries", expired_count)
    if removed is not None:
        logger.info("Signature gate override REVOKED for session=%s (unconsumed)", session_id)


def _consume_payment_gate_override() -> bool:
    """If an override is active for the CURRENT session, consume it and return True.

    Uses the ``_current_payment_session`` context var to identify the session.
    Returns False if no override exists, the session is unknown, or the
    override has expired.
    """
    session_id = _current_payment_session.get() or _get_call_gate_session_id()
    if not session_id:
        return False
    override_token = _get_call_payment_gate_override_token()
    now_monotonic = time.monotonic()
    with _payment_gate_lock:
        expired_count, _evicted_count = _prune_payment_gate_overrides_locked(now_monotonic)
        granted_at = _payment_gate_overrides.pop(session_id, None)
        if granted_at is None and override_token:
            token_granted_at = _payment_gate_override_tokens.get(override_token)
            if token_granted_at is None:
                _payment_gate_override_tokens[override_token] = now_monotonic
                granted_at = now_monotonic
    if expired_count:
        logger.debug("Payment gate override cleanup removed %d stale entries", expired_count)
    if granted_at is None:
        return _consume_payment_gate_override_from_call_meta(session_id, now_monotonic)
    elapsed = now_monotonic - granted_at
    if elapsed > _OVERRIDE_TTL_SECONDS:
        logger.warning(
            "Payment gate override EXPIRED for session=%s (%.1fs > %ds)",
            session_id,
            elapsed,
            int(_OVERRIDE_TTL_SECONDS),
        )
        return False
    logger.info(
        "Payment gate override CONSUMED for session=%s (after %.1fs)",
        session_id,
        elapsed,
    )
    return True


def _consume_payment_gate_override_from_call_meta(session_id: str, now_monotonic: float) -> bool:
    """Consume a stdio browser-process payment override from one-shot MCP metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if not isinstance(user_context, dict):
        return False
    token = user_context.get("payment_gate_override_token")
    if not isinstance(token, str) or not token.strip():
        return False
    token = token.strip()
    with _payment_gate_lock:
        _prune_payment_gate_overrides_locked(now_monotonic)
        if token in _payment_gate_override_tokens:
            return False
        _payment_gate_override_tokens[token] = now_monotonic
    logger.info(
        "Payment gate override ALLOWED from MCP metadata for session=%s (one-shot)",
        session_id,
    )
    return True


def _seed_signature_gate_override_from_call_meta(session_id: str, now_monotonic: float) -> None:
    """Seed a stdio browser-process signature override from MCP metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if not isinstance(user_context, dict):
        return
    token = user_context.get("signature_gate_override_token")
    if not isinstance(token, str) or not token.strip():
        return
    token = token.strip()
    try:
        remaining_actions = max(
            1,
            int(user_context.get("signature_gate_override_actions") or _SIGNATURE_GATE_OVERRIDE_ACTIONS),
        )
    except Exception:
        remaining_actions = _SIGNATURE_GATE_OVERRIDE_ACTIONS

    granted = False
    with _signature_gate_lock:
        _prune_signature_gate_overrides_locked(now_monotonic)
        if token in _signature_gate_override_tokens:
            return
        _signature_gate_override_tokens[token] = now_monotonic
        if session_id not in _signature_gate_overrides:
            _signature_gate_overrides[session_id] = (now_monotonic, remaining_actions)
            granted = True

    if granted:
        logger.info(
            "Signature gate override GRANTED from MCP metadata for session=%s (TTL=%ds, actions=%d)",
            session_id,
            int(_OVERRIDE_TTL_SECONDS),
            remaining_actions,
        )


def _consume_signature_gate_override() -> bool:
    """Consume one signature-gated action allowance for the current session."""
    session_id = _current_signature_session.get() or _get_call_gate_session_id()
    if not session_id:
        return False
    now_monotonic = time.monotonic()
    _seed_signature_gate_override_from_call_meta(session_id, now_monotonic)
    with _signature_gate_lock:
        expired_count, _evicted_count = _prune_signature_gate_overrides_locked(now_monotonic)
        entry = _signature_gate_overrides.get(session_id)
        if entry is None:
            granted_at = None
            remaining = 0
        else:
            granted_at, remaining = entry
            if now_monotonic - granted_at > _OVERRIDE_TTL_SECONDS or remaining <= 1:
                _signature_gate_overrides.pop(session_id, None)
            else:
                _signature_gate_overrides[session_id] = (granted_at, remaining - 1)
    if expired_count:
        logger.debug("Signature gate override cleanup removed %d stale entries", expired_count)
    if granted_at is None:
        return False
    elapsed = now_monotonic - granted_at
    if elapsed > _OVERRIDE_TTL_SECONDS:
        logger.warning(
            "Signature gate override EXPIRED for session=%s (%.1fs > %ds)",
            session_id,
            elapsed,
            int(_OVERRIDE_TTL_SECONDS),
        )
        return False
    logger.info(
        "Signature gate override CONSUMED for session=%s (after %.1fs, remaining=%d)",
        session_id,
        elapsed,
        max(0, remaining - 1),
    )
    return True


# Keywords that indicate payment method selection (case-insensitive).
_PAYMENT_METHOD_KEYWORDS: set[str] = {
    "cash",
    "credit card",
    "debit card",
    "credit/debit",
    "paypal",
    "apple pay",
    "google pay",
    "venmo",
    "affirm",
    "afterpay",
    "klarna",
    "gift card",
    "store credit",
    "bitcoin",
    "crypto",
    "bank transfer",
    "saved card",
    "visa",
    "mastercard",
    "amex",
    "discover",
    "pay with",
    "payment method",
}

# Regex for order-submission / finalization buttons.
_ORDER_SUBMIT_RE = re.compile(
    r"\b(?:place\s+order|submit\s+order|pay\s+now|complete\s+order"
    r"|complete\s+purchase|confirm\s+order|buy\s+now|finalize\s+order"
    r"|process\s+order|proceed\s+to\s+pay)\b",
    re.IGNORECASE,
)


def _payment_block_message(text: str) -> str | None:
    """Return a block-error string if *text* matches a payment gate trigger."""

    if not text:
        return None
    lower = text.strip().lower()

    # Safe-pattern whitelist: skip blocking for known false-positive phrases
    # like "cash back rewards", "Discover more deals", "pay attention", etc.
    if _is_safe_pattern(text):
        return None

    # Check order-submission patterns first (exact regex).
    _is_order_submit = _ORDER_SUBMIT_RE.search(lower)

    # Check payment-method keywords (substring match).
    _is_payment_kw = any(kw in lower for kw in _PAYMENT_METHOD_KEYWORDS)

    if not _is_order_submit and not _is_payment_kw:
        return None

    if _is_order_submit:
        return (
            "PAYMENT METHOD DETECTED: Do NOT click order submission buttons. "
            'Call payment(action="request_review", ...) with merchant, items, total, and delivery context.'
        )

    return (
        "PAYMENT METHOD DETECTED: Do NOT select a payment method. "
        'Call payment(action="request_review", ...) with merchant, items, total, and delivery context.'
    )


async def _is_payment_blocked(text: str) -> str | None:
    """Return a block-error string if *text* matches a payment gate trigger.

    Returns ``None`` if the text is safe to click/select.

    If a one-time override is active (user confirmed the purchase via the
    confirmation page), the override is consumed and the action is allowed.
    """

    blocked = _payment_block_message(text)
    if blocked is None:
        return None
    if _consume_payment_gate_override():
        if _ORDER_SUBMIT_RE.search(text.strip().lower()):
            merchant_submit_block = await _merchant_submit_owner_block_message(text)
            if merchant_submit_block:
                return merchant_submit_block
        logger.info(
            "Payment gate override allowed action through (text=%r)",
            text[:60],
        )
        return None
    return blocked


async def _merchant_submit_owner_block_message(signal: str) -> str | None:
    user_id = _get_call_user_id()
    if not user_id:
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except Exception:
            user_id = None
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async("merchant_submit", user_id=user_id, action="browser_merchant_submit")
    except Exception:
        logger.exception("merchant_submit owner safety control check failed closed")
        return "This capability is temporarily paused while safety controls recover."
    if decision.allowed:
        return None
    logger.warning("Merchant submit blocked by owner safety control (signal=%r)", signal[:80])
    return decision.public_message or "This capability is temporarily paused for safety."


# ---------------------------------------------------------------------------
# Payment gate: safe-pattern whitelist (false-positive mitigation)
# ---------------------------------------------------------------------------
# Phrases that contain payment keywords but are NOT payment actions.
_PAYMENT_SAFE_PATTERNS: tuple[str, ...] = (
    "cash back",
    "cashback",
    "cash reward",
    "pay attention",
    "pay respect",
    "pay tribute",
    "pay it forward",
    "pay per view",
    "pay off",
    "payoff",
    "paid for by",
    "paid in full",
    "repay",
    "payroll",
    "pay stub",
    "paystub",
    "payment history",
    "payment confirmation",  # reading/verifying, not selecting
    "view payment",
    "payment details",  # info display, not selection
    "saved card ending",
    "card ending in",
    "discover more",
    "discover our",
    "discover the",
    "discover new",
    "discover deals",
)


def _is_safe_pattern(text: str) -> bool:
    """Return True if *text* matches a known safe pattern that should NOT
    be blocked by the payment gate (false-positive mitigation)."""
    lower = text.strip().lower()
    return any(safe in lower for safe in _PAYMENT_SAFE_PATTERNS)


def _dedupe_payment_probe_strings(values: list[str]) -> list[str]:
    """Normalize and deduplicate candidate strings for payment-gate checks."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _payment_probe_strings_from_element_info(info: dict[str, Any] | None) -> list[str]:
    """Build payment-gate probe strings from DOM/accessibility metadata."""
    if not isinstance(info, dict):
        return []
    role = str(info.get("role") or "").strip()
    tag = str(info.get("tag") or "").strip()
    name = str(info.get("name") or "").strip()
    primary_text = str(info.get("text") or "")
    candidates = [
        primary_text,
        str(info.get("value") or ""),
        str(info.get("ariaLabel") or ""),
        str(info.get("nameAttr") or ""),
        str(info.get("title") or ""),
        name,
    ]
    if role and name:
        candidates.append("%s %s" % (role, name))
    if role and primary_text:
        candidates.append("%s %s" % (role, primary_text))
    if tag and name:
        candidates.append("%s %s" % (tag, name))
    return _dedupe_payment_probe_strings(candidates)


_SIGNATURE_URL_RE = re.compile(
    r"(?:signature|e-sign|esign|attestation|certification)\b",
    re.IGNORECASE,
)

_SIGNATURE_PAGE_MARKERS: tuple[str, ...] = (
    "constitutes a legal signature",
    "legal signature",
    "checking the box constitutes",
    "checking this box constitutes",
    "by checking this box",
    "i certify",
    "i attest",
    "under penalty of",
    "electronically sign",
    "electronic signature",
    "signature of organizer",
    "signature page",
)

_SIGNATURE_ACTION_MARKERS: tuple[str, ...] = (
    "submit",
    "next",
    "continue",
    "sign",
    "signature",
    "certify",
    "attest",
)


async def _extract_locator_signature_probe_info(locator: Any) -> dict[str, Any]:
    """Return signature-gate probe info from a live locator plus page context."""
    try:
        info = await locator.first.evaluate(
            """el => ({
                text: (el.innerText || '').substring(0, 200),
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
                url: location.href,
                pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
            })""",
            timeout=1500,
        )
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


async def _signature_gate_block_from_locator(locator: Any, *fallback_values: str) -> tuple[str | None, str]:
    """Block sign/submit actions on legal signature pages until approved."""
    info = await _extract_locator_signature_probe_info(locator)
    page_text = str(info.get("pageText") or "")
    url = str(info.get("url") or "")
    probe_strings = _payment_probe_strings_from_element_info(info)
    probe_strings.extend(str(value) for value in fallback_values if isinstance(value, str))
    probe_strings = _dedupe_payment_probe_strings(probe_strings)

    lower_page = page_text.lower()
    lower_url = url.lower()
    page_has_signature_context = bool(_SIGNATURE_URL_RE.search(lower_url)) or any(
        marker in lower_page for marker in _SIGNATURE_PAGE_MARKERS
    )
    if not page_has_signature_context:
        return None, ""

    input_type = str(info.get("inputType") or "").lower()
    role = str(info.get("role") or "").lower()
    is_signature_control = input_type in {"checkbox", "radio"} or role in {
        "checkbox",
        "radio",
        "switch",
    }
    action_signal = ""
    for candidate in probe_strings:
        lower_candidate = candidate.lower()
        if any(marker in lower_candidate for marker in _SIGNATURE_ACTION_MARKERS):
            action_signal = candidate
            break
    if not is_signature_control and not action_signal:
        return None, ""

    control_label = str(info.get("name") or info.get("ariaLabel") or info.get("text") or "")
    signal = control_label if is_signature_control and control_label else (action_signal or control_label)
    if _consume_signature_gate_override():
        logger.info("Signature gate override allowed action through (signal=%r)", signal[:80])
        return None, signal

    if is_signature_control:
        return (
            "LEGAL SIGNATURE DETECTED: Do NOT check signature or certification controls yet. "
            'Call signature(action="request_review", ...) with authority, document, signer, and certification details first.',
            signal,
        )

    return (
        "SIGNATURE GATE BLOCKED: This action would apply a legal signature or continue past a "
        "certification page. "
        'Call signature(action="request_review", ...) with authority, document, signer, and certification details first.',
        signal,
    )


_PAYMENT_FIELD_KEYWORDS: tuple[str, ...] = (
    "card number",
    "cardnumber",
    "cc-number",
    "cc number",
    "expiry",
    "expiration",
    "exp date",
    "exp month",
    "exp year",
    "expmonth",
    "expyear",
    "cvc",
    "cvv",
    "security code",
    "name on card",
    "cardholder",
    "billing zip",
    "billing postal",
)


async def _extract_locator_payment_probe_strings(locator: Any) -> list[str]:
    """Return payment-gate probe strings from a live locator."""
    try:
        info = await locator.first.evaluate(
            """el => ({
                text: (el.innerText || '').substring(0, 200),
                value: ('value' in el && el.value != null) ? String(el.value).substring(0, 200) : '',
                ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 200),
                nameAttr: (el.getAttribute('name') || '').substring(0, 200),
                title: (el.getAttribute('title') || '').substring(0, 200),
                role: (el.getAttribute('role') || '').toLowerCase(),
                tag: (el.tagName || '').toLowerCase(),
                name: (
                    el.getAttribute('aria-label')
                    || el.getAttribute('name')
                    || el.getAttribute('title')
                    || el.innerText
                    || el.textContent
                    || ''
                ).substring(0, 200)
            })""",
            timeout=1500,
        )
    except Exception:
        return []
    return _payment_probe_strings_from_element_info(info)


async def _payment_field_block_from_locator(locator: Any, *fallback_values: str) -> tuple[str | None, str]:
    """Block fills/selects on card-entry fields before secure confirmation."""
    candidates = [value for value in fallback_values if isinstance(value, str)]
    candidates.extend(await _extract_locator_payment_probe_strings(locator))
    for candidate in _dedupe_payment_probe_strings(candidates):
        lower = candidate.lower()
        if any(keyword in lower for keyword in _PAYMENT_FIELD_KEYWORDS):
            return (
                "PAYMENT SAFETY VIOLATION: Do NOT fill payment fields directly. "
                'Call payment(action="request_review", ...) with merchant, total, and order summary first.',
                candidate,
            )
    return None, ""


async def _hidden_payment_iframe_warnings(page: Any) -> list[dict[str, str]]:
    """Return hidden iframes that look like payment field collectors."""

    try:
        frames = await page.evaluate(
            """() => Array.from(document.querySelectorAll('iframe')).map((el, index) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const hidden = (
                    el.hidden || el.getAttribute('aria-hidden') === 'true' ||
                    style.display === 'none' || style.visibility === 'hidden' ||
                    Number(style.opacity) === 0 || rect.width <= 1 || rect.height <= 1
                );
                const attrs = [
                    el.id, el.name, el.title, el.getAttribute('aria-label'),
                    el.getAttribute('src'), el.getAttribute('srcdoc'),
                    el.getAttribute('data-testid'), el.className
                ].filter(Boolean).join(' ').slice(0, 500);
                return { index, hidden, attrs };
            }).filter(frame => frame.hidden && /(?:pan|card.?number|cc.?number|cvv|cvc|payment|billing)/i.test(frame.attrs))""",
            timeout=1500,
        )
    except Exception:
        return []
    if not isinstance(frames, list):
        return []
    return [dict(frame) for frame in frames if isinstance(frame, dict)]


async def _hidden_payment_iframe_block(page: Any, frame_selector: str, target_selector: str) -> str | None:
    """Best-effort phishing guard for hidden payment iframes."""

    probe = "%s %s" % (frame_selector or "", target_selector or "")
    if not any(keyword.replace(" ", "") in probe.lower().replace(" ", "") for keyword in _PAYMENT_FIELD_KEYWORDS):
        return None
    warnings = await _hidden_payment_iframe_warnings(page)
    if not warnings:
        return None
    return (
        "PAYMENT SAFETY VIOLATION: Hidden payment iframe detected. "
        "Do NOT fill payment fields in hidden frames. Call "
        'payment(action="request_review", ...) with merchant, total, and order summary first.'
    )


async def _payment_gate_block_from_locator(locator: Any, *fallback_values: str) -> tuple[str | None, str]:
    """Check a live locator plus fallback strings for signature/payment gated actions."""
    signature_blocked, signature_signal = await _signature_gate_block_from_locator(locator, *fallback_values)
    if signature_blocked:
        return signature_blocked, signature_signal
    candidates = [value for value in fallback_values if isinstance(value, str)]
    candidates.extend(await _extract_locator_payment_probe_strings(locator))
    for candidate in _dedupe_payment_probe_strings(candidates):
        blocked = _payment_block_message(candidate)
        if blocked:
            if _consume_payment_gate_override():
                if _ORDER_SUBMIT_RE.search(candidate.strip().lower()):
                    merchant_submit_block = await _merchant_submit_owner_block_message(candidate)
                    if merchant_submit_block:
                        return merchant_submit_block, candidate
                logger.info(
                    "Payment gate override allowed locator action through (signal=%r)",
                    candidate[:60],
                )
                return None, candidate
            return blocked, candidate
    return None, ""


# ---------------------------------------------------------------------------
# Payment gate: checkout URL detection
# ---------------------------------------------------------------------------
_CHECKOUT_URL_RE = re.compile(
    r"(?:checkout|cart|order|payment|billing|purchase|pay)\b",
    re.IGNORECASE,
)


def _is_checkout_url(url: str) -> bool:
    """Return True if *url* looks like a checkout/payment page."""
    if not url:
        return False
    # Parse just the path + query — ignore domain (e.g. pay.amazon.com counts).
    parsed = urlparse(url)
    check_str = parsed.netloc + parsed.path + (parsed.query or "")
    return bool(_CHECKOUT_URL_RE.search(check_str))


# ---------------------------------------------------------------------------
# Payment gate: JavaScript script scanning
# ---------------------------------------------------------------------------
# Patterns that indicate a JS script is trying to perform payment actions.
# We match: .click() calls on elements that look like payment buttons,
# form.submit() on payment forms, and direct payment-keyword DOM queries
# combined with action methods.

_JS_PAYMENT_ACTION_RE = re.compile(
    # Direct .click() on selectors containing payment keywords
    r"""(?:querySelector(?:All)?\s*\(\s*['"][^'"]*"""
    r"""(?:place.?order|submit.?order|pay.?now|complete.?order"""
    r"""|complete.?purchase|confirm.?order|buy.?now|finalize.?order"""
    r"""|process.?order|proceed.?to.?pay|checkout.?btn|checkout.?button"""
    r"""|payment.?submit|order.?submit)"""
    r"""[^'"]*['"]\s*\)\s*\.(?:click|submit|dispatchEvent))"""
    # .click() combined with payment text in the same expression
    r"""|(?:\.click\s*\(\s*\).*(?:place.?order|submit.?order|pay.?now"""
    r"""|complete.?order|complete.?purchase|confirm.?order|buy.?now"""
    r"""|finalize.?order|process.?order|proceed.?to.?pay))"""
    # Reverse: payment text followed by .click()
    r"""|(?:(?:place.?order|submit.?order|pay.?now|complete.?order"""
    r"""|complete.?purchase|confirm.?order|buy.?now|finalize.?order"""
    r"""|process.?order|proceed.?to.?pay).*\.click\s*\(\s*\))"""
    # form.submit() on checkout/payment forms
    r"""|(?:(?:checkout|payment|order).?form.*\.submit\s*\(\s*\))""",
    re.IGNORECASE | re.DOTALL,
)

# Simpler pattern: any .click() plus any payment keyword in the same script
# (catches things like: `document.querySelector('[text="Place Order"]').click()`)
_JS_CLICK_PAYMENT_KW_RE = re.compile(
    r"\.click\s*\(",
    re.IGNORECASE,
)
_JS_PAYMENT_KW_RE = re.compile(
    r"(?:place.?order|submit.?order|pay.?now|complete.?order"
    r"|complete.?purchase|confirm.?order|buy.?now|finalize.?order"
    r"|process.?order|proceed.?to.?pay)",
    re.IGNORECASE,
)

# Safe JS patterns that should never be blocked (read-only operations).
_JS_SAFE_PATTERNS: tuple[str, ...] = (
    "scrollIntoView",
    "getBoundingClientRect",
    "innerText",
    "textContent",
    "innerHTML",
    "offsetHeight",
    "offsetWidth",
    "scrollTop",
    "scrollLeft",
    "scrollHeight",
    "scrollWidth",
    "window.scrollY",
    "window.scrollX",
    "window.scrollTo",
    "window.scrollBy",
    "getComputedStyle",
    "querySelectorAll",  # reading, not clicking — blocked only when combined with .click()
    "document.title",
    "document.URL",
    "classList.contains",
    "getAttribute",
    "hasAttribute",
    "visibility",
    "display",
    "opacity",
)


async def _is_js_payment_action(script: str) -> str | None:
    """Scan a JavaScript snippet for payment-action patterns.

    Returns a block-error string if the script appears to trigger a payment
    action (clicking order buttons, submitting payment forms, etc.).
    Returns None if the script is safe.

    Respects the one-time payment gate override (user confirmed purchase).
    """
    if not script:
        return None

    # Never block clearly read-only scripts (DOM inspection, scrolling, etc.)
    stripped = script.strip()

    # Block direct DOM value assignment to payment fields (bypasses form-fill guards)
    _PAYMENT_FIELD_JS_RE = re.compile(
        r"""(?:card.?number|cardnumber|cc.?number|cvv|cvc|security.?code|"""
        r"""exp(?:iry|iration|.?month|.?year|.?date)|cardholder|billing.?zip)""",
        re.IGNORECASE,
    )
    if re.search(r"\.value\s*=", stripped) and _PAYMENT_FIELD_JS_RE.search(stripped):
        if not _consume_payment_gate_override():
            return (
                "PAYMENT SAFETY VIOLATION: Do NOT use JavaScript to fill payment "
                "fields (card number, expiry, CVV). Call "
                'payment(action="request_review", ...) with merchant, total, and order summary first.'
            )

    # Also block generic value writes that contain card-like values, even if
    # the selector is just "input" and has no payment keyword.
    _generic_value_write = re.search(
        r"""(?:\.value\s*=|\[['"]value['"]\]\s*=|\.setAttribute\s*\(\s*['"]value['"]\s*,)""",
        stripped,
        re.IGNORECASE,
    )
    if _generic_value_write:
        # Extract quoted values for each quote style separately.  A single
        # cross-quote pattern (['"]...['"]) consumes quotes alternately and
        # misses values nested inside another quote style — e.g. the card
        # number in:  await page.evaluate("...value='4111111111111111'")
        # where the outer "..." swallows the inner '...' boundary.
        quoted_values = re.findall(r"""'([^']{2,80})'""", stripped)
        quoted_values += re.findall(r'''"([^"]{2,80})"''', stripped)
        # Also scan bare digit runs (13-19 digits) so an unquoted card-like
        # literal cannot slip past the quoted-value check.
        quoted_values += re.findall(r"(?<![\d.])\d[\d\s\-]{11,21}\d(?![\d.])", stripped)
        has_payment_value = any(_payment_value_violation(value) for value in quoted_values)
        if has_payment_value or _PAYMENT_FIELD_JS_RE.search(stripped):
            if not _consume_payment_gate_override():
                return (
                    "PAYMENT SAFETY VIOLATION: Do NOT use JavaScript to fill payment "
                    "fields or card-like values. Call "
                    'payment(action="request_review", ...) with merchant, total, and order summary first.'
                )

    # W9 hardening: the W8 regex above only catches `.value=`/`['value']=` /
    # `setAttribute("value", ...)` assignments and only inspects values delimited
    # by matched ASCII quotes.  An LLM can still bypass via backtick template
    # literals, string concatenation (`"4111" + "1111" + ...`), Reflect.set,
    # clipboard.writeText, innerHTML injection, fetch body posting, or
    # `defaultValue`/`document.forms[...].submit()`.  This scan catches a
    # Luhn-valid PAN (or MM/YY expiry literal) anywhere in the script when
    # it is coupled with any DOM/network mutation.
    _js_pan_block = _js_payment_value_bypass(stripped)
    if _js_pan_block:
        if not _consume_payment_gate_override():
            return _js_pan_block

    # If the script has no .click(), .submit(), or dispatchEvent, it's read-only
    if not re.search(r"\.(click|submit|dispatchEvent)\s*\(", stripped, re.IGNORECASE):
        return None

    # Check the explicit payment-action regex first
    _explicit_match = _JS_PAYMENT_ACTION_RE.search(stripped)
    # Check the simpler combined pattern: .click() + payment keyword
    _combined_match = _JS_CLICK_PAYMENT_KW_RE.search(stripped) and _JS_PAYMENT_KW_RE.search(stripped)

    if not _explicit_match and not _combined_match:
        return None

    # Script IS payment-related.  Check the one-time override.
    if _consume_payment_gate_override():
        if _JS_PAYMENT_KW_RE.search(stripped):
            merchant_submit_block = await _merchant_submit_owner_block_message(stripped)
            if merchant_submit_block:
                return merchant_submit_block
        logger.info(
            "Payment gate override allowed JS script through (script=%r)",
            stripped[:80],
        )
        return None

    if _explicit_match:
        return (
            "PAYMENT GATE BLOCKED: This script attempts to click a payment/order "
            "submission element. Do NOT execute JavaScript to bypass payment gates. "
            'Call payment(action="request_review", ...) with merchant, items, total, and delivery context.'
        )

    return (
        "PAYMENT GATE BLOCKED: This script contains .click() combined with "
        "payment/order keywords. Do NOT execute JavaScript to bypass payment gates. "
        'Call payment(action="request_review", ...) with merchant, items, total, and delivery context.'
    )


_JS_SIGNATURE_ACTION_RE = re.compile(
    r"(?:\.click\s*\(|\.submit\s*\(|dispatchEvent\s*\(|\.checked\s*=)",
    re.IGNORECASE,
)
_JS_SIGNATURE_KW_RE = re.compile(
    r"(?:signature|legal.?signature|certif(?:y|ication)|attest|checking this box|constitutes a legal signature)",
    re.IGNORECASE,
)


def _is_js_signature_action(script: str) -> str | None:
    """Scan a JavaScript snippet for legal-signature bypass attempts."""
    stripped = (script or "").strip()
    if not stripped:
        return None
    if not _JS_SIGNATURE_ACTION_RE.search(stripped):
        return None
    if not _JS_SIGNATURE_KW_RE.search(stripped):
        return None
    if _consume_signature_gate_override():
        logger.info(
            "Signature gate override allowed JS script through (script=%r)",
            stripped[:80],
        )
        return None
    return (
        "SIGNATURE GATE BLOCKED: This script attempts to apply a legal signature or continue "
        'past a certification page. Call signature(action="request_review", ...) with authority, '
        "document, signer, and certification details first."
    )


async def _is_js_gated_action(script: str) -> str | None:
    """Return JS gate blockers that do not require live page context."""
    return await _is_js_payment_action(script)


async def _signature_gate_block_from_page(page: Any, script: str) -> str | None:
    """Block generic JS clicks/submits when the current page is a signature page."""
    stripped = (script or "").strip()
    if not stripped:
        return None
    if not _JS_SIGNATURE_ACTION_RE.search(stripped):
        return None
    try:
        page_info = await page.evaluate(
            """() => ({
                url: location.href,
                pageText: ((document.body && document.body.innerText) || '').substring(0, 5000)
            })""",
            timeout=1500,
        )
    except Exception:
        return None
    if not isinstance(page_info, dict):
        return None
    lower_page = str(page_info.get("pageText") or "").lower()
    lower_url = str(page_info.get("url") or "").lower()
    page_has_signature_context = bool(_SIGNATURE_URL_RE.search(lower_url)) or any(
        marker in lower_page for marker in _SIGNATURE_PAGE_MARKERS
    )
    if not page_has_signature_context:
        return None
    if _consume_signature_gate_override():
        logger.info(
            "Signature gate override allowed JS page action through (script=%r)",
            stripped[:80],
        )
        return None
    return (
        "SIGNATURE GATE BLOCKED: This script attempts to apply a legal signature or continue "
        'past a certification page. Call signature(action="request_review", ...) with authority, '
        "document, signer, and certification details first."
    )


# ===========================================================================
# REF-BASED INTERACTION HANDLERS (internal)
# ===========================================================================


async def _do_click_ref(ref: str) -> str:
    """Click an element by its @eN ref. Internal handler for browser_interact(action='click_ref')."""
    if (blocked := _browser_disabled()) is not None:
        return blocked
    # A4: Validate ref format before hitting Playwright
    ref = ref.strip()
    if (invalid := _validate_ref(ref)) is not None:
        return invalid
    try:
        locator = manager.resolve_ref(ref)

        # Capture URL before click so we can detect same-tab navigation.
        page = await manager.get_page()
        state = _get_hint_state(page)
        url_before = page.url
        fingerprint_before = state.last_snapshot_fingerprint
        submit_control_click = False
        submit_click_meta: dict[str, Any] = {}
        dom_fingerprint_before = ""

        # ── Stale-ref early detection ─────────────────────────────────
        # Before attempting any click, verify the ref actually resolves to
        # at least one element on the current page.  If count() == 0 the
        # locator found nothing — the page changed since the last snapshot
        # and the ref is stale.  Return immediately with a helpful message
        # and a fresh snapshot so the LLM can recover without the 5-second
        # Playwright timeout that would otherwise fire.
        try:
            _ref_count = await locator.count()
        except Exception:
            _ref_count = -1  # can't determine — proceed normally

        if _ref_count == 0:
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "ok": False,
                    "stale_ref": True,
                    "error": "Ref @%s was not found on the current page." % ref.lstrip("@"),
                    "requested_ref": "@%s" % ref.lstrip("@"),
                    "current_snapshot_included": True,
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )

        submit_control_click = await _locator_is_submit_control(locator)
        if submit_control_click:
            dom_fingerprint_before = await _playwright_page_fingerprint(page)

        # ── Payment Gate (deterministic) ─────────────────────────────
        # Block clicks on payment-method selectors and order-submission
        # buttons.  This is enforced at the code level so no prompt
        # engineering can bypass it.
        try:
            _btn_text = (await locator.first.inner_text(timeout=1000)).strip()
        except Exception:
            _btn_text = ""
        _ref_meta = manager.get_ref_metadata(ref) or {}
        if not isinstance(_ref_meta, dict):
            _ref_meta = {}
        _meta_name = str(_ref_meta.get("name") or "")
        _meta_role = str(_ref_meta.get("role") or "")
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(
            locator,
            _btn_text,
            _meta_name,
            ("%s %s" % (_meta_role, _meta_name)).strip(),
        )
        if _pay_block:
            logger.warning(
                "Payment gate blocked click on @%s (signal=%r)",
                ref,
                _blocked_signal[:80],
            )
            return _json({"ok": False, "error": _pay_block})

        # ── Wizard Submit guard ──────────────────────────────────────
        # If the user is clicking "Submit" but the page has "Save"
        # buttons, block the click and redirect to Save.  "Submit" on
        # multi-page wizard forms terminates the entire wizard.
        _btn_text_lower = _btn_text.lower()
        if _btn_text_lower == "submit":
            # Check if "Save" or "Next" buttons exist on this page.
            # If either exists, this is a multi-page wizard and "Submit"
            # will terminate it — block and redirect.
            _save_btns = await page.locator("button:has-text('Save')").all_inner_texts()
            _save_btns = [t.strip() for t in _save_btns if t.strip()]
            _next_btns = await page.locator("button:has-text('Next'), input[value*='Next']").all_inner_texts()
            _next_btns = [t.strip() for t in _next_btns if t.strip()]
            if _save_btns or _next_btns:
                snapshot_text = await _take_ref_snapshot(page)
                return _json(
                    {
                        "ok": False,
                        "blocked": True,
                        "error": "'Submit' terminates the wizard.",
                        "wizard_submit_blocked": True,
                        "save_button_labels": _save_btns,
                        "next_button_labels": _next_btns,
                        "snapshot": snapshot_text or "",
                        "url": page.url,
                    }
                )

        # Detect new tabs/popups opened by the click (target="_blank", etc.).
        popup_future, popup_cleanup, prev_url = manager.begin_popup_watch()
        try:
            if submit_control_click:
                submit_click_meta = await _click_submit_with_fallback(
                    locator,
                    page,
                    url_before,
                    dom_fingerprint_before,
                    timeout=5000,
                )
            else:
                await locator.first.click(timeout=5000)
        except Exception as click_err:
            _err_str = str(click_err)
            logger.info(
                "Click on %s failed (%s), attempting recovery",
                ref,
                _err_str[:80],
            )
            # Overlay blocking the element (e.g. OneTrust cookie banner,
            # promo modals).  Retry with force=True which bypasses
            # Playwright's actionability checks and clicks through overlays.
            if "intercepts pointer events" in _err_str:
                logger.info(
                    "Overlay blocking click on %s — retrying with force=True",
                    ref,
                )
                try:
                    if submit_control_click:
                        submit_click_meta = await _click_submit_with_fallback(
                            locator,
                            page,
                            url_before,
                            dom_fingerprint_before,
                            timeout=5000,
                            force=True,
                        )
                    else:
                        await locator.first.click(timeout=5000, force=True)
                except Exception as force_err:
                    # force=True can still fail if element is outside viewport.
                    # Fall through to JS click as last resort.
                    if "outside" in str(force_err) and "viewport" in str(force_err):
                        logger.info(
                            "Element %s also outside viewport — JS click",
                            ref,
                        )
                        try:
                            await locator.first.evaluate("el => { el.scrollIntoView({block:'center'}); el.click(); }")
                        except Exception:
                            popup_cleanup()
                            raise
                    else:
                        popup_cleanup()
                        raise
            elif "outside" in _err_str and "viewport" in _err_str:
                # Element below the fold — scroll into view and click via JS.
                logger.info(
                    "Element %s outside viewport — JS scroll + click",
                    ref,
                )
                try:
                    await locator.first.evaluate("el => { el.scrollIntoView({block:'center'}); el.click(); }")
                except Exception:
                    popup_cleanup()
                    raise
            else:
                popup_cleanup()
                raise

        popup_result = await manager.resolve_popup_watch(
            popup_future,
            popup_cleanup,
            prev_url,
        )

        # Get the (possibly new) active page.
        page = await manager.get_page()
        state = _get_hint_state(page)

        if not popup_result["new_tab"]:
            # Normal same-tab click — wait for possible navigation.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                logger.debug("domcontentloaded wait timed out after ref click")
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                logger.debug("networkidle wait timed out after ref click")

        title = await page.title()
        snapshot_text = await _take_ref_snapshot(page)
        # Detect loading indicators and re-snapshot if content is still loading.
        snapshot_text = await _wait_and_resnap(page, snapshot_text)

        # Detect same-tab navigation (URL changed after click).
        navigated = manager.detect_navigation(url_before)

        result: dict[str, Any] = {
            "clicked": "@%s" % ref.lstrip("@"),
            "title": title,
            "url": page.url,
        }
        if submit_click_meta:
            result.update(submit_click_meta)
        if popup_result.get("new_tab"):
            result["new_tab"] = True
            result["previous_url"] = popup_result["previous_url"]
        if navigated:
            result["navigation_detected"] = True
            result["new_url"] = page.url
        if navigated or popup_result.get("new_tab"):
            result["refs_invalidated"] = True
            result["ref_invalidation_reason"] = "page_changed"
            result["current_snapshot_included"] = bool(snapshot_text)
        if snapshot_text:
            result["snapshot"] = snapshot_text

        if navigated and snapshot_text:
            fill_form_buttons = _fill_form_button_candidates(snapshot_text)
            if fill_form_buttons and "textbox" in snapshot_text:
                result["fill_form_button_candidates"] = fill_form_buttons

        # Post-click validation (Skyvern Validator pattern):
        # If the page content is identical before and after the click,
        # warn the agent so it doesn't loop on a non-functional element.
        # Escalation: 2nd no-effect click on same ref → ok=False (100%
        # compliance).  1st → ok=True warning (77%).
        # Evidence: task c815efb96bd2 — 8x @e13 click, ok=True warning
        # ignored.  Spin detector text injection also ignored (0%).
        _clean_ref = "@%s" % ref.lstrip("@")
        if (
            not navigated
            and not popup_result.get("new_tab")
            and fingerprint_before
            and _fingerprints_equivalent(_snapshot_fingerprint(snapshot_text), fingerprint_before)
        ):
            _identity = _no_effect_identity(_clean_ref, _ref_meta)
            _same_target = _identity == state.last_no_effect_identity or _clean_ref == state.last_no_effect_ref
            state.last_no_effect_count = state.last_no_effect_count + 1 if _same_target else 1
            result["ok"] = False
            result["last_click_no_effect"] = True
            result["no_effect_count"] = state.last_no_effect_count
            result["no_effect_target"] = {
                "ref": _clean_ref,
                "identity": _identity,
            }
            result["snapshot_unchanged_after_click"] = True
            result["url_unchanged_after_click"] = True
            if state.last_no_effect_count >= 2:
                result["retry_blocked"] = True
                result["error"] = "Repeated click on %s had no visible effect." % _clean_ref
            state.last_no_effect_ref = _clean_ref
            state.last_no_effect_identity = _identity
        else:
            _reset_no_effect_state(state)

        state.last_snapshot_fingerprint = _snapshot_fingerprint(snapshot_text or "")

        _url_lower = page.url.lower()
        if navigated and any(kw in _url_lower for kw in ("quit", "exit", "cancel", "abandon")):
            result["wizard_exit_detected"] = True
            result["wizard_exit_url"] = page.url

        if (
            not navigated
            and snapshot_text
            and ("must be filled" in snapshot_text.lower() or "require attention" in snapshot_text.lower())
        ):
            result["validation_error_detected"] = True
            result["required_fields_empty"] = True
            fill_form_buttons = _fill_form_button_candidates(snapshot_text)
            if fill_form_buttons:
                result["fill_form_button_candidates"] = fill_form_buttons
            _err_fields = re.findall(
                r'textbox\s+@e\d+\s+"([^"]+)"' r"[^\n]*\n[^\n]*must be filled",
                snapshot_text,
                re.IGNORECASE,
            )
            if _err_fields:
                result["unfilled_required_field_labels"] = _err_fields[:5]

        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except ValueError as exc:
        # Ref not found — include a fresh snapshot so the LLM can recover.
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "error": str(exc),
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )
        except Exception:
            return _json({"error": str(exc)})
    except Exception as exc:
        # Always try to include a snapshot on failure so the LLM can see
        # what is blocking the click (coupon dialogs, overlays, etc.).
        error_msg = "Click @%s failed: %s" % (ref, exc)
        recovery: dict[str, Any] = {"error": error_msg}
        try:
            page = await manager.get_page()
            recovery["url"] = page.url
            try:
                recovery["title"] = await page.title()
            except Exception:
                logger.debug("Snapshot recovery title lookup failed")
            try:
                snapshot_text = await _take_ref_snapshot(page)
                if snapshot_text:
                    recovery["snapshot"] = snapshot_text
            except Exception:
                # Snapshot also failed — try a lightweight fallback:
                # just get the page title and URL so the LLM isn't blind.
                logger.debug("Snapshot recovery also failed for click error")
        except Exception:
            logger.debug("Snapshot recovery page lookup failed")
        return _json(recovery)


async def _do_fill_ref(ref: str, text: str = "", value: str = "") -> str:
    """Fill a single text input by its @eN ref. Internal handler for browser_interact(action='fill_ref')."""
    # Accept either "text" or "value" — LLMs often use "value" naturally.
    fill_text = text or value
    if not fill_text:
        return _json({"error": "Either 'text' or 'value' is required"})
    try:
        locator = manager.resolve_ref(ref)
        if (violation := _payment_value_violation(fill_text)) is not None:
            return _json({"ok": False, "error": violation})
        # Auto-detect element type for radio/checkbox/select
        tag = ""
        input_type = ""
        role = ""
        try:
            tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
            if tag == "input":
                input_type = await locator.first.evaluate("el => (el.type || '').toLowerCase()", timeout=2000)
            role = await locator.first.evaluate("el => (el.getAttribute('role') || '').toLowerCase()", timeout=2000)
        except Exception:
            logger.debug("Fill-ref element metadata lookup failed")
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, "@%s" % ref.lstrip("@"))
        if _pay_block and input_type in {"radio", "checkbox"}:
            return _json({"ok": False, "error": _pay_block})
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, "@%s" % ref.lstrip("@"))
        if _field_block:
            return _json({"ok": False, "error": _field_block})
        if tag == "select" or role in _ARIA_DROPDOWN_ROLES:
            await _select_dropdown_value(locator, fill_text, "@%s" % ref.lstrip("@"))
        elif input_type == "radio":
            await locator.first.check(timeout=5000)
        elif input_type == "checkbox":
            should_check = fill_text.lower() in ("true", "1", "yes", "on")
            await locator.first.set_checked(should_check, timeout=5000)
        elif _is_text_value_verification_target(tag, input_type) or not tag:
            await _fill_text_locator_verified(locator, fill_text, timeout=5000)
        else:
            return _json(
                {
                    "ok": False,
                    "error": "Ref @%s is not a fillable form field (%s)" % (ref.lstrip("@"), tag or "unknown"),
                }
            )

        page = await manager.get_page()
        title = await page.title()
        snapshot_text = await _take_ref_snapshot(page)
        result: dict[str, Any] = {
            "filled": "@%s" % ref.lstrip("@"),
            "text": fill_text,
            "title": title,
            "url": page.url,
        }
        if snapshot_text:
            result["snapshot"] = snapshot_text
        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except ValueError as exc:
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "error": str(exc),
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )
        except Exception:
            return _json({"error": str(exc)})
    except Exception as exc:
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "error": "Fill @%s failed: %s" % (ref, exc),
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )
        except Exception:
            return _json({"error": "Fill @%s failed: %s" % (ref, exc)})


async def _do_select_ref(ref: str, value: str) -> str:
    """Select an option in a dropdown/combobox by its @eN ref. Internal handler for browser_interact(action='select_ref')."""
    # ── Payment Gate (deterministic) ─────────────────────────────────
    _pay_block = await _is_payment_blocked(value)
    if _pay_block:
        logger.warning(
            "Payment gate blocked select on @%s (value=%r)",
            ref,
            value[:60],
        )
        return _json({"ok": False, "error": _pay_block})

    try:
        locator = manager.resolve_ref(ref)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, "@%s" % ref.lstrip("@"))
        if _field_block:
            return _json({"ok": False, "error": _field_block})
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, "@%s" % ref.lstrip("@"), value)
        if _pay_block:
            return _json({"ok": False, "error": _pay_block})
        await _select_dropdown_value(locator, value, "@%s" % ref.lstrip("@"))

        page = await manager.get_page()
        title = await page.title()
        snapshot_text = await _take_ref_snapshot(page)
        result: dict[str, Any] = {
            "selected": value,
            "ref": "@%s" % ref.lstrip("@"),
            "title": title,
            "url": page.url,
        }
        if snapshot_text:
            result["snapshot"] = snapshot_text
        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except ValueError as exc:
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "error": str(exc),
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )
        except Exception:
            return _json({"error": str(exc)})
    except Exception as exc:
        try:
            page = await manager.get_page()
            snapshot_text = await _take_ref_snapshot(page)
            return _json(
                {
                    "error": "Select @%s failed: %s" % (ref, exc),
                    "snapshot": snapshot_text or "",
                    "url": page.url,
                }
            )
        except Exception:
            return _json({"error": "Select @%s failed: %s" % (ref, exc)})


# Backward-compatible module-level helpers retained for direct imports in
# tests, docs, and local tooling. The agent-facing path uses
# ``browser_interact``; these thin wrappers preserve the older surface.
async def browser_click(selector: str, text: str = "") -> str:
    return await _do_click(selector, text)


async def browser_type(selector: str, text: str, clear_first: bool = True) -> str:
    return await _do_type(selector, text, clear_first)


async def browser_select(selector: str, value: str) -> str:
    return await _do_select(selector, value)


async def browser_click_ref(ref: str) -> str:
    return await _do_click_ref(ref)


async def browser_fill_ref(ref: str, text: str = "", value: str = "") -> str:
    return await _do_fill_ref(ref, text=text, value=value)


async def browser_select_ref(ref: str, value: str) -> str:
    return await _do_select_ref(ref, value)


# ===========================================================================
# COMPOUND INTERACTION TOOL
# ===========================================================================


@server.tool(
    description=(
        "Click, type, select, or press a key on the page. Supports @eN refs "
        "from browser_snapshot and CSS or visible-text fallback selectors. "
        "Click results include raw change-detection fields such as "
        "last_click_no_effect, no_effect_count, retry_blocked, navigated, "
        "new_fields, and snapshot. " + _REF_USAGE_DOC + _SELECTOR_SYNTAX_DOC
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_interact(
    action: Annotated[
        str,
        Field(
            description=(
                "Interaction type. PREFERRED: 'click_ref', 'fill_ref', "
                "'select_ref' — use the @eN ref from your most recent "
                "browser_snapshot. Fallback (only when refs are unavailable): "
                "'click' (CSS/text element click), "
                "'type' (type into input by CSS or @eN ref), "
                "'select' (select dropdown option by CSS or @eN ref), "
                "'press_key' (press Enter, Tab, Escape, ArrowDown, etc.). "
                "'click_ref' clicks element by @eN ref from snapshot. "
                "'fill_ref' fills input by @eN ref — auto-detects field type. "
                "'select_ref' selects dropdown by @eN ref — handles ARIA listbox/combobox."
            )
        ),
    ],
    selector: Annotated[
        str,
        Field(
            description=(
                "CSS selector or plain text for 'click', 'type', 'select' actions. "
                "Examples: '#submit-btn', 'button.primary', 'Add to Cart'. "
                "Do NOT use jQuery-style pseudo-selectors (':contains', "
                "'[innerText=...]'); they are not valid CSS and are rejected. "
                + _SELECTOR_SYNTAX_DOC
                + "Prefer @eN refs from a fresh browser_snapshot. "
                "Ignored for ref-based actions (click_ref, fill_ref, select_ref)."
            )
        ),
    ] = "",
    ref: Annotated[
        str,
        Field(
            description=(
                "Element ref like 'e5' or '@e5' from a browser_snapshot. "
                "Required for 'click_ref', 'fill_ref', 'select_ref' actions. "
                "Ignored for selector-based actions (click, type, select)."
            )
        ),
    ] = "",
    text: Annotated[
        str,
        Field(
            description=(
                "Text to type for 'type' and 'fill_ref' actions. "
                "For 'click' action: optional text filter to disambiguate CSS matches. "
                "For 'fill_ref': alias for value (use either text or value)."
            )
        ),
    ] = "",
    value: Annotated[
        str,
        Field(
            description=(
                "Option value or label for 'select' and 'select_ref' actions. "
                "For 'fill_ref': alias for text (use either text or value)."
            )
        ),
    ] = "",
    key: Annotated[
        str,
        Field(description="Keyboard key for action='press_key', such as Enter, Tab, Escape, or ArrowDown."),
    ] = "",
    clear_first: Annotated[
        bool,
        Field(
            description=("For 'type' action only: whether to clear existing field content before typing. Default True.")
        ),
    ] = True,
) -> str:
    """Interact with browser elements using a single compound tool.

    Consolidates click, type, and select operations for both CSS selectors
    and @eN accessibility refs.

    Examples:
    - browser_interact(action="click", selector="Add to Cart")
    - browser_interact(action="type", selector="#search", text="laptop")
    - browser_interact(action="select", selector="#state", value="California")
    - browser_interact(action="click_ref", ref="e5")
    - browser_interact(action="fill_ref", ref="e3", text="John Doe")
    - browser_interact(action="select_ref", ref="e4", value="California")
    - browser_interact(action="press_key", key="Enter")
    """
    if action == "click":
        if ref and not selector:
            return await _do_click_ref(ref)
        if not selector:
            return _json({"error": "selector is required for action='click'"})
        if re.match(r"^@?e\d+$", selector.strip()):
            return await _do_click_ref(selector.strip())
        return await _do_click(selector, text)

    if action == "type":
        if ref and not selector:
            return await _do_fill_ref(ref, text, value)
        if not selector:
            return _json({"error": "selector is required for action='type'"})
        if not text:
            return _json({"error": "text is required for action='type'"})
        if re.match(r"^@?e\d+$", selector.strip()):
            return await _do_fill_ref(selector.strip(), text, value)
        return await _do_type(selector, text, clear_first)

    if action == "select":
        if ref and not selector:
            if not value:
                return _json({"error": "value is required for action='select'"})
            return await _do_select_ref(ref, value)
        if not selector:
            return _json({"error": "selector is required for action='select'"})
        if not value:
            return _json({"error": "value is required for action='select'"})
        if re.match(r"^@?e\d+$", selector.strip()):
            return await _do_select_ref(selector.strip(), value)
        return await _do_select(selector, value)

    if action == "click_ref":
        if not ref:
            return _json({"error": "ref is required for action='click_ref'"})
        return await _do_click_ref(ref)

    if action == "fill_ref":
        if not ref:
            return _json({"error": "ref is required for action='fill_ref'"})
        return await _do_fill_ref(ref, text, value)

    if action == "select_ref":
        if not ref:
            return _json({"error": "ref is required for action='select_ref'"})
        if not value:
            return _json({"error": "value is required for action='select_ref'"})
        return await _do_select_ref(ref, value)

    if action == "press_key":
        key_name = key or value or text
        if not key_name:
            return _json({"error": "key is required for action='press_key'"})
        return await browser_press_key(key_name)

    return _json(
        {
            "error": "Unknown action '%s'. Use one of: click, type, select, click_ref, fill_ref, select_ref, press_key"
            % action
        }
    )


@server.tool(
    description=(
        "Fill multiple form fields in one call using @eN refs, auto-detecting field type (text, select, radio, checkbox)."
        + _REF_USAGE_DOC
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_fill_form(fields: list[FormField]) -> str:
    """Fill ALL form INPUT FIELDS on the page in one call. Auto-detects type.

    This fills form fields ONLY — text inputs, textareas, dropdowns, radio
    buttons, checkboxes. It does NOT click buttons, links, or submit forms.
    Use browser_interact(action='click_ref') for submit/next/continue buttons after filling.

    Include EVERY form element in one call — text fields AND radio buttons
    AND checkboxes. Don't leave out radio buttons!

    For radio buttons: use value "true" to select.
    For checkboxes: "true"/"yes" to check, "false"/"no" to uncheck.
    For dropdowns: use the visible label text as value.

    Common failure (40% rate): including submit buttons in the fields list.
    Only include actual INPUT fields. If a field doesn't fill, the ref may
    be stale — take a fresh browser_snapshot to get updated refs.

    After filling: take a browser_snapshot to verify fields were filled
    correctly BEFORE clicking submit with browser_interact(action='click_ref').

    Args:
        fields: List of fields to fill. Each needs ref and value.
    """
    if (blocked := _browser_disabled()) is not None:
        return blocked
    if not fields:
        return _json({"error": "fields list must not be empty"})

    # A4: Validate and strip all ref formats before execution
    for _f in fields:
        if isinstance(_f, dict):
            raw_ref = str(_f.get("ref", "") or "").strip()
            _f["ref"] = raw_ref
        else:
            raw_ref = _f.ref.strip()
            _f.ref = raw_ref
        if (invalid := _validate_ref(raw_ref)) is not None:
            return invalid

    # --- Payment safety gate: reject credit card numbers ---
    # The agent must NEVER fill card numbers directly.  Detect card-like
    # values (13-19 digits with optional spaces/dashes) and reject the
    # entire call with a structured payment-review-required error.
    _CARD_RE = re.compile(r"^[\d\s\-]{13,19}$")
    # Fix 19: Also detect payment METHOD selection.  Evidence: task
    # 0929025bc954 tried to select "Credit Card" from a dropdown at
    # Dominos checkout instead of requesting payment review.
    _PAYMENT_METHODS = frozenset(
        {
            "credit card",
            "debit card",
            "credit/debit card",
            "paypal",
            "apple pay",
            "venmo",
            "google pay",
            "affirm",
            "afterpay",
            "klarna",
        }
    )
    for entry in fields:
        val = entry.get("value", "") if isinstance(entry, dict) else getattr(entry, "value", "")
        digits = re.sub(r"[\s\-]", "", val)
        if _CARD_RE.match(val) and digits.isdigit() and len(digits) >= 13:
            return _json(
                {
                    "ok": False,
                    "error": (
                        "PAYMENT SAFETY VIOLATION: You must NEVER type credit card numbers. "
                        'Call payment(action="request_review", ...) with merchant, total, and order summary '
                        "before filling or selecting any payment fields."
                    ),
                    "error_category": "payment_review_required",
                    "required_tool": "payment",
                    "required_action": "request_review",
                }
            )
        if val.lower().strip() in _PAYMENT_METHODS:
            return _json(
                {
                    "ok": False,
                    "error": (
                        "PAYMENT METHOD DETECTED: Do NOT select a payment method. "
                        'Call payment(action="request_review", ...) with merchant, total, and order summary first.'
                    ),
                    "error_category": "payment_review_required",
                    "required_tool": "payment",
                    "required_action": "request_review",
                }
            )

    filled: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for entry in fields:
        # Support both FormField objects and raw dicts (backward compat)
        if isinstance(entry, dict):
            ref = entry.get("ref", "")
            value = entry.get("value", "")
            is_select = entry.get("select", False)
        else:
            ref = entry.ref
            value = entry.value
            is_select = entry.select or False
        if not ref or not value:
            errors.append({"ref": ref, "error": "ref and value are required"})
            continue
        try:
            locator = manager.resolve_ref(ref)
            # Auto-detect element type: route to the correct interaction
            # method based on the actual DOM element, regardless of what the
            # caller specified.  Handles <select>, <input type=radio/checkbox>.
            tag = ""
            input_type = ""
            role = ""
            try:
                tag = await locator.first.evaluate("el => el.tagName.toLowerCase()", timeout=2000)
                if tag == "input":
                    input_type = await locator.first.evaluate("el => (el.type || '').toLowerCase()", timeout=2000)
                role = await locator.first.evaluate("el => (el.getAttribute('role') || '').toLowerCase()", timeout=2000)
            except Exception:
                logger.debug("Dropdown fill-ref element metadata lookup failed")
            use_select = is_select or tag == "select" or role in _ARIA_DROPDOWN_ROLES
            if use_select:
                await _select_dropdown_value(locator, value, "@%s" % ref.lstrip("@"))
            elif input_type == "radio":
                # Radio buttons must be clicked/checked, not filled.
                await locator.first.check(timeout=5000)
            elif input_type == "checkbox":
                # Checkboxes: check if value is truthy, uncheck otherwise.
                should_check = value.lower() in ("true", "1", "yes", "on")
                await locator.first.set_checked(should_check, timeout=5000)
            elif _is_text_value_verification_target(tag, input_type) or not tag:
                await _fill_text_locator_verified(locator, value, timeout=5000)
            else:
                # Non-fillable element (link, button, span, div, etc.)
                # Skip gracefully — don't error, just note it.
                errors.append(
                    {
                        "ref": "@%s" % ref.lstrip("@"),
                        "error": "not a form field (%s)" % (tag or "unknown"),
                    }
                )
                continue
            filled.append({"ref": "@%s" % ref.lstrip("@"), "value": value})
        except Exception as exc:
            errors.append({"ref": "@%s" % ref.lstrip("@"), "error": str(exc)[:200]})

    # If nothing was filled, report failure so the agent doesn't treat it as
    # a silent success and waste further steps on no-ops.
    if not filled:
        attempted_refs = []
        for entry in fields:
            if isinstance(entry, dict):
                r = entry.get("ref", "")
            else:
                r = entry.ref
            if r:
                attempted_refs.append("@%s" % r if not r.startswith("@") else r)
        return _json(
            {
                "ok": False,
                "filled": [],
                "attempted_refs": attempted_refs,
                "errors": errors,
                "error": "None of the specified refs were fillable text fields.",
            }
        )

    # One snapshot after ALL fills (saves tokens vs snapshot-per-fill).
    try:
        page = await manager.get_page()
        title = await page.title()
        snapshot_text = await _take_ref_snapshot(page)
        has_errors = bool(errors)
        result: dict[str, Any] = {
            "ok": not has_errors,
            "filled": filled,
            "title": title,
            "url": page.url,
        }
        if has_errors:
            failed_refs = [e["ref"] for e in errors]
            result["errors"] = errors
            result["error"] = "One or more fields were not filled."
            result["field_error_count"] = len(errors)
            result["failed_refs"] = failed_refs
        if snapshot_text:
            result["snapshot"] = snapshot_text

        # Skipped-button escalation — ok=False error forces 100% model
        # compliance.  ok=True hints only reach 77%.  Evidence:
        #   task f42d87b75bfb — hint absent (uncommitted), model never clicked
        #   task 7e50579dc2d9 — ok=True hint ignored, model claimed success
        # Proven hierarchy: ok=False error > ok=True hint > text injection.
        _skipped_buttons = [f for f in filled if f.get("skipped") and "button" in f["skipped"]]
        if _skipped_buttons and snapshot_text:
            # Don't echo the model's ref — it may be wrong (refs shift
            # after typing). Instead, find the actual submit/search button
            # in the fresh snapshot. Evidence: task adf0ee8a4aa0 — hint
            # said "click @e10" but @e10 was "Clear", not "Search-Button".
            _submit_match = re.search(
                r'button\s+(@e\d+)\s+"([^"]*(?:Search|Submit|Go|Find|Apply|Send|Next|Continue)[^"]*)"',
                snapshot_text,
                re.IGNORECASE,
            )
            result["ok"] = False
            result["error"] = "Buttons cannot be filled."
            result["filled_but_not_submitted"] = True
            if _submit_match:
                result["submit_button_candidates"] = [{"ref": _submit_match.group(1), "label": _submit_match.group(2)}]

        # Detect unselected radio button groups on the page.  Many checkout
        # forms require a payment method radio button to be selected before
        # submission.  If radio buttons exist and none in a group are checked,
        # nudge the agent to select one.
        if snapshot_text and "radio" in snapshot_text.lower():
            has_unchecked_radio = (
                "radio @" in snapshot_text and "[checked]" not in snapshot_text.lower().split("radio")[0]
            )
            # Simple heuristic: if any radio line exists, check if any are checked
            radio_lines = [ln for ln in snapshot_text.split("\n") if "radio @" in ln.lower()]
            checked_radios = [ln for ln in radio_lines if "[checked]" in ln.lower()]
            if radio_lines and not checked_radios:
                result["unselected_radio_group_detected"] = True
                result["radio_button_count"] = len(radio_lines)
                result["radio_button_refs"] = re.findall(r"(@e\d+)", "\n".join(radio_lines))

        await _enrich_auto_screenshot(page, result)
        return _json(result)
    except Exception as exc:
        return _json(
            {
                "ok": not bool(errors),
                "filled": filled,
                "errors": errors,
                "error": "Snapshot failed: %s" % exc,
            }
        )


# ===========================================================================
# VISUAL TOOLS
# ===========================================================================


@server.tool(
    description=("Capture a PNG screenshot of the browser viewport (or full page) as base64 for visual inspection."),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_screenshot(full_page: bool = False) -> str:
    """Take a screenshot for visual inspection (images, layout, CAPTCHA). Prefer browser_snapshot for text.

    Args:
        full_page: Capture full scrollable page (default false = viewport only).
    """
    import base64

    if block := _payment_gate_observation_refusal("browser_screenshot"):
        return block
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_screenshot", page):
            return block
        png_bytes = await page.screenshot(full_page=full_page)
        blank_payload = _black_screenshot_payload(png_bytes)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        title = await page.title()
        if blank_payload:
            blank_payload.update(
                {
                    "title": title,
                    "url": page.url,
                    "image_base64": b64,
                    "mime_type": "image/png",
                }
            )
            return _json(blank_payload)
        return _json(
            {
                "ok": True,
                "title": title,
                "url": page.url,
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
        "Wait for a CSS selector to become visible on the page, useful after actions that trigger async content loading."
        + _SELECTOR_SYNTAX_DOC
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
        page = await manager.get_page()
        timeout = min(max(timeout, 500), 15000)  # clamp 0.5-15s
        try:
            await page.wait_for_selector(selector, timeout=timeout, state="visible")
            count = await page.locator(selector).count()
            text_preview = ""
            if count > 0:
                text_preview = await page.locator(selector).first.inner_text()
                text_preview = text_preview[:200]
            return _json(
                {
                    "found": True,
                    "selector": selector,
                    "count": count,
                    "text_preview": text_preview,
                }
            )
        except Exception:
            return _json(
                {
                    "found": False,
                    "selector": selector,
                    "message": "Element did not appear within %dms" % timeout,
                }
            )
    except Exception as exc:
        return _json({"error": "Wait failed: %s" % exc})


@server.tool(
    description=(
        "Execute a JavaScript expression in the browser and return its JSON-serializable result, truncated to 4000 chars. "
        "Use for DOM text matching or XPath when CSS selectors cannot express a text search."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_evaluate(script: str) -> str:
    """Run raw JavaScript in the browser and return the result. Use for DOM inspection, data extraction, or element discovery (NOT browser_run_script).

    Args:
        script: JavaScript expression or IIFE. Must return a JSON-serializable value.
    """
    if block := _payment_gate_observation_refusal("browser_evaluate"):
        return block

    # ── Length limit ──
    if len(script) > _MAX_JS_LENGTH:
        return _json(
            {"error": "JavaScript code exceeds maximum length (%d chars, max %d)" % (len(script), _MAX_JS_LENGTH)}
        )

    # ── Blocked JS patterns (multi-layered: normalization + case-insensitive + obfuscation) ──
    _blocked_match = await _is_js_blocked(script)
    if _blocked_match:
        return _json({"error": "Blocked JS pattern '%s' — credential/storage access not permitted" % _blocked_match})

    # ── Payment Gate: block JS that clicks payment/order buttons ──────
    _js_block = await _is_js_gated_action(script)
    if _js_block:
        logger.warning(
            "Action gate blocked browser_evaluate (script=%r)",
            script[:120],
        )
        return _json({"ok": False, "error": _js_block})

    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_evaluate", page):
            return block
        _sig_block = await _signature_gate_block_from_page(page, script)
        if _sig_block:
            logger.warning("Signature gate blocked browser_evaluate (script=%r)", script[:120])
            return _json({"ok": False, "error": _sig_block})
        result = await page.evaluate(script)
        # Truncate large results
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "...(truncated)"
            result = json.loads(result_str.rsplit(",", 1)[0] + "]")
        return _json({"result": result, "url": page.url})
    except Exception as exc:
        return _json({"error": "Evaluate failed: %s" % exc})


# ===========================================================================
# SCRIPT EXECUTION
# ===========================================================================

# ---------------------------------------------------------------------------
# Regex dispatch table for browser_run_script
# ---------------------------------------------------------------------------

# Quote-aware argument patterns: use backreference so 'foo("bar")' matches
# correctly (opening quote = single, so we match until the next single quote,
# allowing double quotes inside the argument).
_Q1 = r"""(["'])(.+?)\1"""  # one quoted arg with backreference
_Q2 = r"""(["'])(.+?)\3"""  # second quoted arg (backrefs shift: \3 matches group 3)

# Optional trailing JS options object: {timeout: 5000}, {force: true}, etc.
# Appended to regex patterns for commands that accept Playwright options.
_OPT = r"(?:\s*,\s*\{[^}]*\})?"

_GOTO_RE = re.compile(r"await page\.goto\(" + _Q1 + r"\)")
_CLICK_RE = re.compile(r"await page\.click\(" + _Q1 + _OPT + r"\)")
_LOCATOR_CLICK_RE = re.compile(r"await page\.locator\(" + _Q1 + r"\)\.click\((?:\{[^}]*\})?\)")
_FILL_RE = re.compile(r"await page\.fill\(" + _Q1 + r",\s*" + _Q2 + _OPT + r"\)")
_TYPE_RE = re.compile(r"await page\.type\(" + _Q1 + r",\s*" + _Q2 + _OPT + r"\)")
_PRESS_RE = re.compile(r"await page\.press\(" + _Q1 + r",\s*" + _Q2 + _OPT + r"\)")
_WAIT_SELECTOR_RE = re.compile(r"await page\.wait_for_selector\(" + _Q1 + _OPT + r"\)")
_WAIT_TIMEOUT_RE = re.compile(r"await page\.wait_for_timeout\((\d+)\)")
_SELECT_RE = re.compile(r"await page\.select_option\(" + _Q1 + r",\s*" + _Q2 + _OPT + r"\)")
_CHECK_RE = re.compile(r"await page\.check\(" + _Q1 + _OPT + r"\)")
_UNCHECK_RE = re.compile(r"await page\.uncheck\(" + _Q1 + _OPT + r"\)")
_EVALUATE_RE = re.compile(r"await page\.evaluate\(" + _Q1 + r"\)")
_SCREENSHOT_RE = re.compile(r"await page\.screenshot\(\)")
_TITLE_RE = re.compile(r"await page\.title\(\)")
_URL_RE = re.compile(r"page\.url")
_INNER_TEXT_RE = re.compile(r"await page\.inner_text\(" + _Q1 + _OPT + r"\)")
_GET_ATTR_RE = re.compile(r"await page\.get_attribute\(" + _Q1 + r",\s*" + _Q2 + _OPT + r"\)")
_GO_BACK_RE = re.compile(r"await page\.go_back\(\)")
_GO_FORWARD_RE = re.compile(r"await page\.go_forward\(\)")
_RELOAD_RE = re.compile(r"await page\.reload\(\)")
_SLEEP_RE = re.compile(r"await asyncio\.sleep\((\d+(?:\.\d+)?)\)")

# frame_locator chain: page.frame_locator('sel').locator('sel').<action>(<args>)
# Captures: frame selector, locator selector, then a chain of .method(args) calls.
# Note: matched after removeprefix("await "), so no "await " prefix in regex.
_FRAME_LOCATOR_RE = re.compile(
    r"page\.frame_locator\(([\"'])(.+?)\1\)"
    r"\.locator\(([\"'])(.+?)\3\)"
    r"((?:\.\w+\([^)]*\))*)"  # chained methods + terminal
    r"\s*$"
)

# Parser for JS options objects: {timeout: 5000, force: true, delay: 100}
# Supports numbers, booleans, and quoted strings.
_JS_OPT_ENTRY_RE = re.compile(r"""(\w+)\s*:\s*(?:(\d+(?:\.\d+)?)|"([^"]*)"|'([^']*)'|(true|false))""")

# Trailing options pattern for stripping from lines before regex dispatch.
_TRAILING_OPTS_RE = re.compile(r",\s*(\{[^}]*\})\s*\)$")
# Locator-style: options inside .click({...})
_PAREN_OPTS_RE = re.compile(r"\((\{[^}]*\})\s*\)$")

# Whitelisted Playwright option keys (security: only pass safe options).
_SAFE_OPTION_KEYS = frozenset(
    {
        "timeout",
        "force",
        "delay",
        "button",
        "no_wait_after",
        "strict",
        "position",
        "modifiers",
        "trial",
    }
)


def _parse_js_options(opts_str: str) -> dict[str, Any]:
    """Parse a JS options object like ``{timeout: 5000, force: true}``.

    Only whitelisted keys are returned.
    """
    result: dict[str, Any] = {}
    for m in _JS_OPT_ENTRY_RE.finditer(opts_str):
        key = m.group(1)
        if key not in _SAFE_OPTION_KEYS:
            continue
        if m.group(2) is not None:
            num_str = m.group(2)
            result[key] = float(num_str) if "." in num_str else int(num_str)
        elif m.group(3) is not None:
            result[key] = m.group(3)
        elif m.group(4) is not None:
            result[key] = m.group(4)
        elif m.group(5) is not None:
            result[key] = m.group(5) == "true"
    return result


def _extract_options(line: str) -> dict[str, Any]:
    """Extract Playwright options from a command line.

    Looks for trailing ``{key: value}`` before the closing paren.
    """
    m = _TRAILING_OPTS_RE.search(line)
    if m:
        return _parse_js_options(m.group(1))
    m = _PAREN_OPTS_RE.search(line)
    if m:
        return _parse_js_options(m.group(1))
    return {}


async def _is_js_blocked(script: str) -> str | None:
    """Multi-layered JS blocklist check.

    Returns the blocked pattern string if blocked, else None.
    Delegates to the shared safety implementation used by browser transports.
    """
    return blocked_js_pattern(script)


_MAX_JS_LEN = 500
_MAX_JS_LENGTH = 5000  # Max length for browser_evaluate / browser_run_script

# ---------------------------------------------------------------------------
# Locator chain parser — handles page.locator(...).first().click(),
# page.get_by_text(...).click(), page.get_by_role('button', name='X').fill('Y')
# ---------------------------------------------------------------------------

# Whitelist of allowed entry methods, chain methods, and terminal actions.
_LOCATOR_ENTRY_METHODS = frozenset(
    {
        "locator",
        "get_by_text",
        "get_by_role",
        "get_by_label",
        "get_by_placeholder",
    }
)
_LOCATOR_CHAIN_METHODS = frozenset({"first", "last", "nth"})
_LOCATOR_TERMINAL_ACTIONS = frozenset(
    {
        "click",
        "fill",
        "check",
        "uncheck",
        "select_option",
    }
)

# Master regex: matches page.locator(...) or page.get_by_*(...) followed by
# one or more chained calls ending with a terminal action.
# Note: matched after removeprefix("await "), so no "await " prefix in regex.
_LOCATOR_CHAIN_RE = re.compile(
    r"page\.(locator|get_by_text|get_by_role|get_by_label|get_by_placeholder)"
    r"\((.+?)\)"  # capture args of entry method
    r"((?:\.\w+\([^)]*\))*)"  # zero or more chained .method() calls
    r"\s*$"
)


def _unquote(s: str) -> str:
    """Strip surrounding quotes from a string argument."""
    s = s.strip()
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def _parse_entry_args(method: str, raw_args: str) -> tuple[list[Any], dict[str, Any]]:
    """Parse positional and keyword arguments for locator entry methods.

    Returns (positional_args, keyword_args).
    """
    args: list[Any] = []
    kwargs: dict[str, Any] = {}

    if method == "get_by_role":
        # get_by_role('button', name='Submit') — positional role + optional name=
        parts = [p.strip() for p in raw_args.split(",")]
        for part in parts:
            if "=" in part and not part.strip().startswith(("'", '"')):
                key, _, val = part.partition("=")
                kwargs[key.strip()] = _unquote(val)
            else:
                args.append(_unquote(part))
    else:
        # locator, get_by_text, get_by_label, get_by_placeholder — single string arg
        args.append(_unquote(raw_args))

    return args, kwargs


def _parse_chain_segment(segment: str) -> tuple[str, list[str]]:
    """Parse a single chain segment like '.first()' or '.nth(2)'.

    Returns (method_name, args_list).
    """
    m = re.match(r"\.(\w+)\(([^)]*)\)", segment)
    if not m:
        raise ValueError("Cannot parse chain segment: %s" % segment)
    method = m.group(1)
    raw = m.group(2).strip()
    args = [raw] if raw else []
    return method, args


async def _execute_locator_chain(page: Any, line: str, opts: dict[str, Any]) -> str:
    """Execute a locator chain like page.locator('sel').first().click().

    Builds the locator step-by-step from parsed segments. No eval.
    """
    m = _LOCATOR_CHAIN_RE.match(line.strip().removeprefix("await ").strip())
    if not m:
        raise ValueError("Cannot parse locator chain: %s" % line[:80])

    entry_method = m.group(1)
    entry_args_raw = m.group(2)
    chain_str = m.group(3)  # e.g. ".first().click()" or ".nth(2).fill('hello')"

    if entry_method not in _LOCATOR_ENTRY_METHODS:
        raise ValueError("Disallowed locator entry method: %s" % entry_method)

    # Security: check string arguments for blocked patterns (multi-layered)
    _loc_blocked = await _is_js_blocked(entry_args_raw) or await _is_js_blocked(chain_str)
    if _loc_blocked:
        raise RuntimeError("Blocked pattern '%s' in locator chain" % _loc_blocked)

    # Build the locator from entry method
    pos_args, kw_args = _parse_entry_args(entry_method, entry_args_raw)
    locator = getattr(page, entry_method)(*pos_args, **kw_args)

    # Parse chain segments: split ".method(args)" segments
    segments = re.findall(r"\.\w+\([^)]*\)", chain_str)
    if not segments:
        raise ValueError("Locator chain has no terminal action: %s" % line[:80])

    # Last segment is the terminal action; preceding ones are chain methods
    for seg in segments[:-1]:
        method_name, seg_args = _parse_chain_segment(seg)
        if method_name not in _LOCATOR_CHAIN_METHODS:
            raise ValueError(
                "Disallowed chain method: '%s'. Allowed: %s" % (method_name, ", ".join(sorted(_LOCATOR_CHAIN_METHODS)))
            )
        if method_name == "nth":
            if not seg_args or not seg_args[0].isdigit():
                raise ValueError("nth() requires an integer argument")
            locator = locator.nth(int(seg_args[0]))
        elif method_name == "first":
            locator = locator.first  # property in Playwright Python
        elif method_name == "last":
            locator = locator.last  # property in Playwright Python

    # Terminal action
    terminal_name, terminal_args = _parse_chain_segment(segments[-1])
    if terminal_name not in _LOCATOR_TERMINAL_ACTIONS:
        raise ValueError(
            "Disallowed terminal action: '%s'. Allowed: %s"
            % (terminal_name, ", ".join(sorted(_LOCATOR_TERMINAL_ACTIONS)))
        )

    kw: dict[str, Any] = {"timeout": 5000}
    kw.update(opts)

    desc_parts = [entry_method, "(", entry_args_raw, ")"]
    for seg in segments[:-1]:
        desc_parts.append(seg)
    desc = "".join(desc_parts)

    if terminal_name == "click":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.click(**kw)
        return "Clicked %s" % desc
    elif terminal_name == "fill":
        if not terminal_args:
            raise ValueError("fill() requires a text argument")
        text = _unquote(terminal_args[0])
        if (violation := _payment_value_violation(text)) is not None:
            raise RuntimeError(violation)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, desc)
        if _field_block:
            raise RuntimeError(_field_block)
        await locator.fill(text, **kw)
        return "Filled %s with '%s'" % (desc, text)
    elif terminal_name == "check":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.check(**kw)
        return "Checked %s" % desc
    elif terminal_name == "uncheck":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.uncheck(**kw)
        return "Unchecked %s" % desc
    elif terminal_name == "select_option":
        if not terminal_args:
            raise ValueError("select_option() requires a value argument")
        val = _unquote(terminal_args[0])
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, val, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, desc)
        if _field_block:
            raise RuntimeError(_field_block)
        await locator.select_option(val, **kw)
        return "Selected '%s' in %s" % (val, desc)

    raise ValueError("Unhandled terminal action: %s" % terminal_name)


async def _execute_frame_locator_chain(page: Any, line: str, opts: dict[str, Any]) -> str:
    """Execute a frame_locator chain like page.frame_locator('iframe').locator('sel').click().

    Builds a FrameLocator, then chains a Locator, then applies the terminal action.
    """
    m = _FRAME_LOCATOR_RE.match(line.strip().removeprefix("await ").strip())
    if not m:
        raise ValueError("Cannot parse frame_locator chain: %s" % line[:80])

    frame_sel = m.group(2)  # e.g. 'iframe'
    loc_sel = m.group(4)  # e.g. 'text=Start a New Business'
    chain_str = m.group(5)  # e.g. '.click()' or '.fill("hello")'

    # Security: check for blocked patterns (multi-layered)
    _frame_blocked = await _is_js_blocked(frame_sel) or await _is_js_blocked(loc_sel) or await _is_js_blocked(chain_str)
    if _frame_blocked:
        raise RuntimeError("Blocked pattern '%s' in frame_locator chain" % _frame_blocked)

    _hidden_iframe_block = await _hidden_payment_iframe_block(page, frame_sel, loc_sel)
    if _hidden_iframe_block:
        raise RuntimeError(_hidden_iframe_block)

    # Build frame_locator → locator chain
    frame_loc = page.frame_locator(frame_sel)
    locator = frame_loc.locator(loc_sel)

    # Parse chain segments
    segments = re.findall(r"\.\w+\([^)]*\)", chain_str)
    if not segments:
        raise ValueError("frame_locator chain has no terminal action: %s" % line[:80])

    # Intermediate chain methods (first, last, nth)
    for seg in segments[:-1]:
        method_name, seg_args = _parse_chain_segment(seg)
        if method_name not in _LOCATOR_CHAIN_METHODS:
            raise ValueError(
                "Disallowed chain method: '%s'. Allowed: %s" % (method_name, ", ".join(sorted(_LOCATOR_CHAIN_METHODS)))
            )
        if method_name == "nth":
            if not seg_args or not seg_args[0].isdigit():
                raise ValueError("nth() requires an integer argument")
            locator = locator.nth(int(seg_args[0]))
        elif method_name == "first":
            locator = locator.first
        elif method_name == "last":
            locator = locator.last

    # Terminal action
    terminal_name, terminal_args = _parse_chain_segment(segments[-1])
    if terminal_name not in _LOCATOR_TERMINAL_ACTIONS:
        raise ValueError(
            "Disallowed terminal action: '%s'. Allowed: %s"
            % (terminal_name, ", ".join(sorted(_LOCATOR_TERMINAL_ACTIONS)))
        )

    kw: dict[str, Any] = {"timeout": 5000}
    kw.update(opts)

    desc = "frame_locator('%s').locator('%s')" % (frame_sel, loc_sel)

    if terminal_name == "click":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.click(**kw)
        return "Clicked %s" % desc
    elif terminal_name == "fill":
        if not terminal_args:
            raise ValueError("fill() requires a text argument")
        text = _unquote(terminal_args[0])
        if (violation := _payment_value_violation(text)) is not None:
            raise RuntimeError(violation)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, desc)
        if _field_block:
            raise RuntimeError(_field_block)
        await locator.fill(text, **kw)
        return "Filled %s with '%s'" % (desc, text)
    elif terminal_name == "check":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.check(**kw)
        return "Checked %s" % desc
    elif terminal_name == "uncheck":
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        await locator.uncheck(**kw)
        return "Unchecked %s" % desc
    elif terminal_name == "select_option":
        if not terminal_args:
            raise ValueError("select_option() requires a value argument")
        val = _unquote(terminal_args[0])
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(locator, val, desc)
        if _pay_block:
            raise RuntimeError(_pay_block)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(locator, desc)
        if _field_block:
            raise RuntimeError(_field_block)
        await locator.select_option(val, **kw)
        return "Selected '%s' in %s" % (val, desc)

    raise ValueError("Unhandled terminal action: %s" % terminal_name)


async def _execute_playwright_line(page: Any, line: str) -> str | None:
    """Execute a single Playwright command by parsing and dispatching.

    Returns a human-readable result string, or None for void operations.
    Raises ValueError for unrecognized commands, RuntimeError for blocked
    patterns, and propagates Playwright exceptions for failed operations.
    """
    stripped = line.strip()

    # --- Navigation ---
    m = _GOTO_RE.match(stripped)
    if m:
        url = m.group(2)
        # SSRF check on goto URLs inside scripts too.
        url_error = _validate_url(url if "://" in url else "https://" + url)
        if url_error:
            raise RuntimeError("SSRF blocked: %s" % url_error)
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return "Navigated to %s" % url

    m = _GO_BACK_RE.match(stripped)
    if m:
        await page.go_back(timeout=10000, wait_until="domcontentloaded")
        return "Went back to %s" % page.url

    m = _GO_FORWARD_RE.match(stripped)
    if m:
        await page.go_forward(timeout=10000, wait_until="domcontentloaded")
        return "Went forward to %s" % page.url

    m = _RELOAD_RE.match(stripped)
    if m:
        await page.reload(timeout=10000, wait_until="domcontentloaded")
        return "Reloaded %s" % page.url

    # Extract JS options ({timeout: 5000}, {force: true}, etc.) for dispatch.
    opts = _extract_options(stripped)

    # --- Clicking ---
    m = _CLICK_RE.match(stripped)
    if m:
        # Payment gate: block clicks on payment/order selectors
        _click_sel = m.group(2)
        _click_locator = page.locator(_click_sel)
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_click_locator, _click_sel)
        if _pay_block and not _is_safe_pattern(_click_sel):
            raise RuntimeError(_pay_block)
        kw: dict[str, Any] = {"timeout": 5000}
        kw.update(opts)
        await page.click(_click_sel, **kw)
        return "Clicked '%s'" % _click_sel

    m = _LOCATOR_CLICK_RE.match(stripped)
    if m:
        # Payment gate: block clicks on payment/order selectors
        _loc_sel = m.group(2)
        _loc = page.locator(_loc_sel)
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, _loc_sel)
        if _pay_block and not _is_safe_pattern(_loc_sel):
            raise RuntimeError(_pay_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.locator(_loc_sel).click(**kw)
        return "Clicked locator '%s'" % _loc_sel

    # Frame locator chains: page.frame_locator('iframe').locator('sel').click()
    if stripped.startswith("await page.frame_locator("):
        return await _execute_frame_locator_chain(page, stripped, opts)

    # Locator chains: page.locator(...).first().click(), page.get_by_text(...).click(), etc.
    if stripped.startswith("await page.locator(") or stripped.startswith("await page.get_by_"):
        return await _execute_locator_chain(page, stripped, opts)

    # --- Text input ---
    m = _FILL_RE.match(stripped)
    if m:
        _fill_sel = m.group(2)
        _fill_text = m.group(4)
        if (violation := _payment_value_violation(_fill_text)) is not None:
            raise RuntimeError(violation)
        _loc = page.locator(_fill_sel)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, _fill_sel)
        if _field_block:
            raise RuntimeError(_field_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.fill(_fill_sel, _fill_text, **kw)
        return "Filled '%s' with '%s'" % (_fill_sel, _fill_text)

    m = _TYPE_RE.match(stripped)
    if m:
        _type_sel = m.group(2)
        _type_text = m.group(4)
        if (violation := _payment_value_violation(_type_text)) is not None:
            raise RuntimeError(violation)
        _loc = page.locator(_type_sel)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, _type_sel)
        if _field_block:
            raise RuntimeError(_field_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.type(_type_sel, _type_text, **kw)
        return "Typed '%s' into '%s'" % (_type_text, _type_sel)

    m = _PRESS_RE.match(stripped)
    if m:
        _press_sel = m.group(2)
        _press_key = m.group(4)
        if _press_key.lower() in {"enter", "numpadenter", "space"}:
            _loc = page.locator(_press_sel)
            _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, _press_sel, _press_key)
            if _pay_block:
                raise RuntimeError(_pay_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.press(_press_sel, _press_key, **kw)
        return "Pressed '%s' on '%s'" % (_press_key, _press_sel)

    # --- Waiting ---
    m = _WAIT_SELECTOR_RE.match(stripped)
    if m:
        kw = {"timeout": 10000}
        kw.update(opts)
        await page.wait_for_selector(m.group(2), **kw)
        return "Selector '%s' appeared" % m.group(2)

    m = _WAIT_TIMEOUT_RE.match(stripped)
    if m:
        await page.wait_for_timeout(int(m.group(1)))
        return "Waited %s ms" % m.group(1)

    m = _SLEEP_RE.match(stripped)
    if m:
        duration = float(m.group(1))
        if duration > 30:
            raise ValueError("asyncio.sleep duration capped at 30 seconds")
        await asyncio.sleep(duration)
        return "Slept %.1f s" % duration

    # --- Form controls ---
    m = _SELECT_RE.match(stripped)
    if m:
        _select_sel = m.group(2)
        _select_val = m.group(4)
        _loc = page.locator(_select_sel)
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, _select_sel, _select_val)
        if _pay_block:
            raise RuntimeError(_pay_block)
        _field_block, _blocked_signal = await _payment_field_block_from_locator(_loc, _select_sel)
        if _field_block:
            raise RuntimeError(_field_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.select_option(_select_sel, _select_val, **kw)
        return "Selected '%s' in '%s'" % (_select_val, _select_sel)

    m = _CHECK_RE.match(stripped)
    if m:
        _check_sel = m.group(2)
        _loc = page.locator(_check_sel)
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, _check_sel)
        if _pay_block:
            raise RuntimeError(_pay_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.check(_check_sel, **kw)
        return "Checked '%s'" % _check_sel

    m = _UNCHECK_RE.match(stripped)
    if m:
        _uncheck_sel = m.group(2)
        _loc = page.locator(_uncheck_sel)
        _pay_block, _blocked_signal = await _payment_gate_block_from_locator(_loc, _uncheck_sel)
        if _pay_block:
            raise RuntimeError(_pay_block)
        kw = {"timeout": 5000}
        kw.update(opts)
        await page.uncheck(_uncheck_sel, **kw)
        return "Unchecked '%s'" % _uncheck_sel

    # --- JavaScript evaluation ---
    m = _EVALUATE_RE.match(stripped)
    if m:
        js_code = m.group(2)
        if len(js_code) > _MAX_JS_LEN:
            raise ValueError("JS expression too long (%d chars, max %d)" % (len(js_code), _MAX_JS_LEN))
        _js_blocked_match = await _is_js_blocked(js_code)
        if _js_blocked_match:
            raise RuntimeError("Blocked JS pattern '%s' — credential/storage access not permitted" % _js_blocked_match)
        # Payment gate: block JS that clicks payment/order buttons
        _js_block = await _is_js_gated_action(js_code)
        if _js_block:
            raise RuntimeError(_js_block)
        _sig_block = await _signature_gate_block_from_page(page, js_code)
        if _sig_block:
            raise RuntimeError(_sig_block)
        result = await page.evaluate(_normalize_browser_run_script_js(js_code))
        if error_text := _browser_eval_error_text(result):
            raise RuntimeError(error_text)
        result_str = json.dumps(result, default=str, ensure_ascii=False)
        return _truncate(result_str, 500)

    # --- Screenshots ---
    m = _SCREENSHOT_RE.match(stripped)
    if m:
        await page.screenshot()
        return "Screenshot taken"

    # --- Reading page state ---
    m = _TITLE_RE.match(stripped)
    if m:
        title = await page.title()
        return title

    m = _URL_RE.match(stripped)
    if m:
        return page.url

    m = _INNER_TEXT_RE.match(stripped)
    if m:
        kw = {"timeout": 5000}
        kw.update(opts)
        text = await page.inner_text(m.group(2), **kw)
        return _truncate(text, 500)

    m = _GET_ATTR_RE.match(stripped)
    if m:
        kw = {"timeout": 5000}
        kw.update(opts)
        val = await page.get_attribute(m.group(2), m.group(4), **kw)
        return str(val)

    raise ValueError(
        "Unrecognized command: '%s'. Supported: page.goto, page.click, "
        "page.fill, page.type, page.press, page.wait_for_selector, "
        "page.wait_for_timeout, page.select_option, page.check, page.uncheck, "
        "page.evaluate, page.screenshot, page.title, page.url, page.inner_text, "
        "page.get_attribute, page.go_back, page.go_forward, page.reload, "
        "page.locator(...).click, page.locator(...).first().click(), "
        "page.locator(...).nth(N).click(), page.get_by_text(...).click(), "
        "page.get_by_role(...).click(), page.get_by_label(...).fill(), "
        "page.get_by_placeholder(...).fill(), "
        "page.frame_locator(...).locator(...).click(), asyncio.sleep" % stripped[:80]
    )


@server.tool(
    description=(
        "Execute browser code. For simple DOM reads pass JavaScript directly, e.g. "
        "document.title, return document.title, evaluate(document.title), or evaluate document.title. "
        "For multi-step actions pass Playwright lines like await page.click('#submit'). "
        "JavaScript exceptions return success=false instead of opaque success output."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
)
async def browser_run_script(script: str) -> str:
    """Execute a JavaScript expression/read or a multi-step Playwright script.

    Plain JavaScript is accepted directly. Natural evaluate forms such as
    ``return document.title``, ``evaluate(document.title)``, and
    ``evaluate document.title`` are normalized before evaluation. Multi-step
    scripts can use lines starting with ``await page.`` / ``page.`` /
    ``await asyncio.sleep``.

    Args:
        script: Multi-line Playwright commands. REQUIRED — do not omit.
    """
    if block := _payment_gate_observation_refusal("browser_run_script"):
        return block

    # Belt-and-suspenders validation: LLMs sometimes ignore schema constraints
    # and send empty tool_input {}. Catch it here with an instructive error.
    if not script or not script.strip():
        return _json(
            {
                "error": "Missing script parameter.",
                "missing_parameter": "script",
                "expected_input": "Playwright commands or a JavaScript expression.",
                "success": False,
            }
        )

    # ── Length limit ──
    if len(script) > _MAX_JS_LENGTH:
        return _json(
            {
                "error": "JavaScript code exceeds maximum length (%d chars, max %d)" % (len(script), _MAX_JS_LENGTH),
                "success": False,
            }
        )

    # ── Payment Gate: scan entire script for payment bypass attempts ───
    # The model can embed JS payment actions across multiple lines.
    # Scan the whole script as a single string first.
    _js_block = await _is_js_gated_action(script)
    if _js_block:
        logger.warning(
            "Action gate blocked browser_run_script (script=%r)",
            script[:200],
        )
        return _json({"ok": False, "error": _js_block, "success": False})

    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("browser_run_script", page):
            return block
        _sig_block = await _signature_gate_block_from_page(page, script)
        if _sig_block:
            logger.warning("Signature gate blocked browser_run_script (script=%r)", script[:200])
            return _json({"ok": False, "error": _sig_block, "success": False})
        results: list[str] = []

        # Parse lines: strip whitespace, skip blanks and comments.
        lines = [l.strip() for l in script.strip().split("\n") if l.strip() and not l.strip().startswith("#")]

        if not lines:
            return _json({"error": "Script is empty (no executable lines)"})

        if not any("page." in line or "asyncio.sleep" in line for line in lines):
            _blocked_match = await _is_js_blocked(script)
            if _blocked_match:
                return _json(
                    {
                        "ok": False,
                        "error": "Blocked JS pattern '%s' -- credential/storage access not permitted" % _blocked_match,
                        "success": False,
                    }
                )
            normalized_script = _normalize_browser_run_script_js(script)
            evaluated = await page.evaluate(normalized_script)
            if error_text := _browser_eval_error_text(evaluated):
                return _json(
                    {
                        "ok": False,
                        "error": error_text,
                        "output": error_text,
                        "success": False,
                        "url": page.url,
                    }
                )
            result_text = json.dumps(evaluated, ensure_ascii=False, default=str)
            if len(result_text) > 4000:
                result_text = result_text[:4000] + "...(truncated)"
            return _json(
                {
                    "result": evaluated,
                    "output": result_text,
                    "success": True,
                    "url": page.url,
                }
            )

        for i, line in enumerate(lines):
            try:
                result = await _execute_playwright_line(page, line)
                results.append("Line %d OK: %s" % (i + 1, line[:80]))
                if result is not None:
                    results.append("  -> %s" % str(result)[:200])
            except Exception as e:
                results.append("Line %d FAILED: %s" % (i + 1, line[:80]))
                results.append("  Error: %s: %s" % (type(e).__name__, str(e)[:200]))
                # Get accessibility snapshot with refs at failure point.
                try:
                    snapshot_text = await _take_ref_snapshot(page)
                    if snapshot_text:
                        results.append("\nPage state at failure:")
                        results.append(snapshot_text)
                except Exception:
                    logger.debug("Could not capture snapshot at script failure point")
                return _json(
                    {
                        "output": "\n".join(results),
                        "completed": i,
                        "total": len(lines),
                        "success": False,
                    }
                )

        # All lines succeeded — append final page state with refs.
        try:
            snapshot_text = await _take_ref_snapshot(page)
            if snapshot_text:
                results.append("\nAll %d lines executed successfully." % len(lines))
                results.append("\nFinal page state:")
                results.append(snapshot_text)
            else:
                results.append("\nAll %d lines executed successfully." % len(lines))
        except Exception:
            results.append("\nAll %d lines executed successfully." % len(lines))

        return _json(
            {
                "output": "\n".join(results),
                "completed": len(lines),
                "total": len(lines),
                "success": True,
            }
        )
    except Exception as exc:
        return _json(
            {
                "ok": False,
                "error": "Script execution failed: %s" % exc,
                "success": False,
            }
        )


# ===========================================================================
# VERIFICATION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Return the current page ARIA snapshot together with the requested assertion. "
        "No runtime pass/fail classifier is applied."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def verify_state(assertion: str) -> str:
    """Return current page state context for model-side assessment."""
    try:
        page = await manager.get_page()
        if block := await _browser_payment_observation_refusal("verify_state", page):
            return block
        snapshot_text = await _take_ref_snapshot(page)
        if not snapshot_text:
            return _json({"error": "No accessibility snapshot available for verification"})
        return _json({"assertion": assertion, "snapshot": snapshot_text, "url": page.url})
    except Exception as exc:
        return _json({"error": "Verification failed: %s" % exc})


# ===========================================================================
# SESSION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Check if the browser is running and get the current URL and page title as a lightweight liveness check."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_status() -> str:
    """Check browser status: running, current URL, page count."""
    if block := _payment_gate_observation_refusal("browser_status"):
        return block
    try:
        user_id = _get_call_user_id()
        running = manager.is_running_for_user(user_id)
        if not isinstance(running, bool):
            running = bool(getattr(manager, "is_running", False))
        if not running:
            return _json({"running": False, "url": None, "pages": 0})
        page = await manager.get_page(user_id)
        return _json({"running": True, "url": page.url, "title": await page.title()})
    except Exception as exc:
        return _json({"error": "Status check failed: %s" % exc})


@server.tool(
    description=(
        "Get HTTP requests and responses captured during the browser session for API discovery, returning the last 20 entries."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_get_api_log(domain_filter: str = "") -> str:
    """Get captured API calls from the current browser session.

    Returns HTTP requests/responses captured during browsing. Useful for
    discovering APIs that can be called directly in future tasks.

    Args:
        domain_filter: Optional domain to filter by (e.g., 'example.com').
                      Empty string returns all captured calls.
    """
    try:
        user_id = _get_call_user_id()
        if not user_id:
            return _json({"message": "No API calls captured in this session.", "count": 0})
        if not manager.is_running_for_user(user_id):
            return _json({"message": "No API calls captured in this session.", "count": 0})
        page = await manager.get_page(user_id)
        if block := await _browser_payment_observation_refusal("browser_get_api_log", page):
            return block
        # Multi-tenant: pass the calling user's id so we can only ever
        # read this tenant's captured entries — never another user's
        # because the process browser is shared between requests.
        log = manager.get_api_log(domain_filter or None, user_id=user_id)
        if not log:
            return _json({"message": "No API calls captured in this session.", "count": 0})

        formatted = [format_api_log_entry(entry) for entry in log[-20:]]

        return _json(
            {
                "count": len(log),
                "showing": len(formatted),
                "entries": formatted,
            }
        )
    except Exception as exc:
        return _json({"error": "Failed to get API log: %s" % exc})


@server.tool(
    description=(
        "Close the browser and release all resources; the next browser tool call will auto-launch a fresh instance."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def browser_close() -> str:
    """Close the browser. Next tool call will launch a fresh instance."""
    try:
        user_id = _get_call_user_id()
        _reset_hint_state(user_id=user_id)
        await manager.shutdown(user_id)
        return _json({"closed": True})
    except Exception as exc:
        return _json({"error": "Close failed: %s" % exc})


# ===========================================================================
# CAPTCHA SOLVING
# ===========================================================================


# ===========================================================================
# Payment form filling (needs direct page access)
# ===========================================================================


@server.tool(
    description=(
        "Fill confirmed payment card details into the final payment page securely; card numbers and CVC are never exposed to the agent."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "irreversible": True, "irreversible_class": "payment"},
)
async def fill_payment_details(card_label: str = "") -> str:
    """ONLY call on the FINAL PAYMENT PAGE where credit card / CVC fields
    are visible in the page snapshot. If you are on a menu, cart, address,
    or checkout summary page WITHOUT card input fields, do NOT call this —
    keep navigating. Only call after Payment-Gate has approved the selected
    card. You will not see the card number or CVC.

    Args:
        card_label: Label of the saved card to use. If empty, uses default.
    """
    try:
        from intent.tools.payment_fill import handle_fill_payment_details

        confirmation_meta = _get_call_payment_confirmation()
        fill_args: dict[str, Any] = {
            "card_label": card_label or confirmation_meta.get("card_label") or None,
        }
        for key in (
            "confirmation_token",
            "cvc_one_shot",
            "payment_session_id",
            "ceiling_override",
        ):
            if confirmation_meta.get(key) is not None:
                fill_args[key] = confirmation_meta[key]
        # Session-cumulative ceiling alignment (safety core — payments, #2767):
        # the check reads spend under ``payment_session_id`` while spend is
        # RECORDED under the executor's ``_payment_gate_session_id()``. On the
        # model-driven autofill path the code-supplied confirmation meta does not
        # carry ``payment_session_id``, so without this fallback the check looked
        # up an empty key, always saw spend=0, and the cumulative cap never
        # tripped. ``_current_payment_session`` is set by the executor to
        # ``self._session_id or task_id`` on every tool dispatch — the SAME string
        # the record path keys on — so it aligns the check key with the record
        # key within the session.
        if not fill_args.get("payment_session_id"):
            active_session = _current_payment_session.get()
            if active_session:
                fill_args["payment_session_id"] = active_session
        # Use get_page() like all other browser tools - ensures browser is
        # launched, page is created/recovered, and async lock is acquired.
        # Direct _page access bypassed all of this and returned None when
        # the page hadn't been created yet (even if browser was running).
        user_id = _require_call_user_id("fill_payment_details")
        try:
            page = await manager.get_page(user_id)
        except Exception:
            page = None

        from core.user_context import user_scope

        with user_scope(user_id):
            return await handle_fill_payment_details(
                fill_args,
                page=page,
            )
    except Exception as exc:
        logger.exception("fill_payment_details failed")
        # Boundary redaction (#583): this string is returned straight to the
        # model and persisted into the task trace. Playwright's own fill /
        # locator errors carry the SELECTOR and waiting steps, never the fill
        # value (verified against playwright-python: locator errors are shaped
        # `Could not resolve {selector} to DOM Element` / timeout call-logs),
        # so the card PAN is not in the exception text today. But this is the
        # single model-facing choke point for the whole payment-fill path
        # (vault lookup, confirmation recovery, one-shot secret consume) — if
        # any current or future upstream ever raised with card data in its
        # message, unredacted `str(exc)` would leak it. redact_card_data is the
        # same key-aware + Luhn card/CVC scrubber the trace writer and log
        # formatter use, applied here so the boundary is defense-in-depth.
        from intent.log_redaction import redact_card_data

        return "Error filling payment details: %s" % redact_card_data(str(exc))


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


def create_browser_server() -> FastMCP:
    """Return the configured browser tools MCP server instance."""
    return server


def get_tool_count() -> int:
    """Return number of registered MCP tools."""
    return len(server._tool_manager._tools)
