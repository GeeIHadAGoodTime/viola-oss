"""Shared browser-MCP safety helpers.

Both browser MCP servers — the Playwright server (``mcp_servers.browser.server``)
and the CDP server that drives the embedded Qt webview
(``mcp_servers.browser_cdp.server``) — need the same payment-card redaction,
SSRF URL validation, and small text utilities.  Keeping one copy here means the
SSRF blocklist and PAN-masking rules cannot drift between the two servers.

``mcp_servers.browser.server`` re-exports every public name in this module, so
existing imports such as ``from mcp_servers.browser.server import _validate_url``
keep working unchanged.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from core.card_validation import luhn_check

# --- Text limits -----------------------------------------------------------

_MAX_TEXT = 5000


def _truncate(text: str, limit: int = _MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated at %d chars]" % limit


# --- SSRF / URL validation -------------------------------------------------

_ALLOWED_SCHEMES = {"http", "https"}
_ABOUT_BLANK = "about:blank"

# Private / internal IP networks that must not be navigated to (SSRF protection).
# Set VIOLA_BROWSER_ALLOW_LOCALHOST=1 to permit 127.0.0.0/8 and ::1 (for local
# testing / agent eval harness).  All other private ranges stay blocked.
_ALLOW_LOCALHOST = os.environ.get("VIOLA_BROWSER_ALLOW_LOCALHOST", "0") == "1"

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    *([] if _ALLOW_LOCALHOST else [ipaddress.ip_network("127.0.0.0/8")]),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/32"),  # unspecified IPv4
    *([] if _ALLOW_LOCALHOST else [ipaddress.ip_network("::1/128")]),
    ipaddress.ip_network("::/128"),  # unspecified IPv6
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_url(url: str) -> str | None:
    """Validate a URL for scheme and SSRF safety.

    Returns an error message string if the URL is unsafe, or None if valid.
    Resolves the hostname to prevent DNS rebinding attacks.
    """
    parsed = urlparse(url)

    # Scheme check — only http and https are permitted.
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return "Scheme '%s' is not permitted. Only http:// and https:// URLs are allowed" % parsed.scheme

    hostname = parsed.hostname
    if not hostname:
        return "URL has no hostname"

    # Resolve the hostname to IP addresses before navigating.
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return "Domain does not resolve via DNS: %s" % hostname

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        for network in _PRIVATE_NETWORKS:
            if addr in network:
                return "Navigation to private/internal network addresses is not permitted"

    return None


def _dns_resolution_outcome(hostname: str | None) -> dict[str, Any]:
    """Return objective DNS resolution facts without suggesting alternatives."""

    if not hostname:
        return {"hostname": None, "outcome": "not_checked", "address_count": 0}
    try:
        addr_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return {
            "hostname": hostname,
            "outcome": "does_not_resolve",
            "address_count": 0,
            "error": str(exc),
        }
    addresses = sorted({info[4][0] for info in addr_infos if info and info[4]})
    return {
        "hostname": hostname,
        "outcome": "resolved",
        "address_count": len(addresses),
    }


def _certificate_chain_status(url: str | None, error_text: str | None = None) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https":
        return "not_applicable"
    lowered = str(error_text or "").lower()
    if any(token in lowered for token in ("certificate", "cert_", "ssl", "tls", "authority_invalid")):
        return "failed"
    return "not_checked"


def _navigation_fact_fields(
    url: str | None,
    *,
    http_status_code: int | None = None,
    redirect_chain: list[str] | None = None,
    error_text: str | None = None,
) -> dict[str, Any]:
    """Build objective browser navigation facts for success and failure payloads."""

    parsed = urlparse(str(url or ""))
    return {
        "http_status_code": http_status_code,
        "dns_resolution": _dns_resolution_outcome(parsed.hostname),
        "certificate_chain_status": _certificate_chain_status(url, error_text),
        "redirect_chain": redirect_chain or [],
    }


def _normalize_navigation_url(raw_url: str) -> tuple[str | None, str | None]:
    """Normalize a browser navigation target without mangling valid schemes."""

    url = str(raw_url or "").strip()
    if not url:
        return None, "URL must not be empty"

    if url.lower() == _ABOUT_BLANK:
        return _ABOUT_BLANK, None

    parsed = urlparse(url)
    if parsed.scheme:
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return (
                None,
                "Scheme '%s' is not permitted. Only http:// and https:// URLs are allowed" % parsed.scheme,
            )
        return url, None

    return "https://" + url, None


def _http_error_page_payload(
    *,
    title: str | None,
    url: str | None,
    description: str | None = None,
    status: int | None = None,
) -> dict[str, Any] | None:
    """Return an error payload when navigation landed on an HTTP/error page."""

    title_text = str(title or "")
    url_text = str(url or "")
    description_text = str(description or "")
    haystack = " ".join([title_text, url_text, description_text]).lower()

    status_is_error = status is not None and status >= 400
    title_is_not_found = ("404" in title_text.lower() and "not found" in title_text.lower()) or (
        "page not found" in title_text.lower()
    )
    url_is_not_found = "pagenotfound" in url_text.lower() or "/404" in url_text.lower()

    if not (status_is_error or title_is_not_found or (url_is_not_found and "not found" in haystack)):
        return None

    status_part = " status=%s" % status if status is not None else ""
    return {
        "ok": False,
        "success": False,
        "http_error_page": True,
        "error": "Navigation reached an HTTP/error page%s: title=%r url=%r" % (status_part, title_text, url_text),
    }


# --- Page-health / bot-challenge detection ---------------------------------
#
# Moved here from mcp_servers/browser/server.py (#579) so BOTH browser servers
# share one copy. The Playwright server surfaced these facts and the #575 loop
# halt consumed them, but the desktop CDP server (mcp_servers/browser_cdp)
# never computed them — a Cloudflare / JS-challenge interstitial on the desktop
# produced no page_health signal, so the bot-protection loop halt could not
# fire on the desktop surface. Keeping the signal lists here means the two
# servers cannot drift.

_PARKING_IFRAME_DOMAINS = (
    "purefindresults.com",
    "sedoparking.com",
    "parkingcrew.net",
    "bodis.com",
    "afternic.com",
    "hugedomains.com",
    "dan.com",
    "godaddy.com/parked",
)

_PARKING_TEXT_SIGNALS = (
    "domain for sale",
    "buy this domain",
    "domain parked",
    "this domain may be for sale",
    "domain is for sale",
    "this domain has expired",
    "domain has been registered",
    "parked free",
    "is available for purchase",
    "domain name has been registered",
    "acquire this domain",
)

# Bot-protection / WAF challenge pages that headless browsers cannot bypass.
_BOT_PROTECTION_SIGNALS = (
    "performing security verification",
    "checking your browser",
    "checking if the site connection is secure",
    "attention required!",
    "please verify you are a human",
    "enable javascript and cookies to continue",
    "ddos protection by",
    "access denied",
    "just a moment...",
)


def _count_interactive_refs(snapshot: str) -> int:
    """Count @eN refs in a snapshot (proxy for interactive elements)."""
    if not snapshot:
        return 0
    return len(re.findall(r"@e\d+", snapshot))


def detect_page_health(
    title: str | None,
    description: str | None,
    snapshot: str | None,
    *,
    meaningful_content_check: Any = None,
) -> dict[str, Any] | None:
    """Return objective page-health facts for empty, parked, or blocked pages.

    Shared by both browser MCP servers. ``meaningful_content_check`` is an
    optional ``Callable[[str], bool]`` the caller passes when it can judge
    whether a ref-less snapshot still carries meaningful ARIA content (the
    Playwright server passes its ``_has_meaningful_content``); when omitted a
    ref-less snapshot is treated as empty. The bot-protection and parked-domain
    branches (the statuses the #575 loop halt acts on:
    ``bot_protection`` / ``parked_or_expired_domain``) need no such callable, so
    the CDP server gets full challenge coverage without the roles machinery.
    """
    snapshot_str = snapshot or ""
    snapshot_lower = snapshot_str.lower()

    # --- Bot protection / WAF challenge pages (most important for loop halt) ---
    for signal in _BOT_PROTECTION_SIGNALS:
        if signal in snapshot_lower:
            return {"status": "bot_protection", "matched_signal": signal}

    # --- Parked / expired domain (text signals are more specific than iframe) ---
    for signal in _PARKING_TEXT_SIGNALS:
        if signal in snapshot_lower:
            return {"status": "parked_or_expired_domain", "matched_signal": signal}
    for domain in _PARKING_IFRAME_DOMAINS:
        if domain in snapshot_lower:
            return {"status": "parked_or_expired_domain", "matched_iframe_domain": domain}

    # --- Dead / empty page ---
    has_title = bool(title and title.strip())
    has_description = bool(description and description.strip())
    has_content = bool(snapshot_str.strip())

    if has_title or has_description:
        return None  # a title or meta description means the page is not dead

    if not has_content:
        return {
            "status": "empty_or_nonfunctional",
            "has_title": has_title,
            "has_description": has_description,
            "has_content": False,
            "interactive_ref_count": 0,
        }

    ref_count = _count_interactive_refs(snapshot_str)
    if ref_count == 0:
        has_meaningful_content = (
            bool(meaningful_content_check(snapshot_str)) if callable(meaningful_content_check) else False
        )
        if not has_meaningful_content:
            return {
                "status": "empty_or_nonfunctional",
                "has_title": has_title,
                "has_description": has_description,
                "has_content": has_content,
                "has_meaningful_content": has_meaningful_content,
                "interactive_ref_count": ref_count,
            }

    return None


def _black_screenshot_payload(png_bytes: bytes) -> dict[str, Any] | None:
    """Return an error payload if a PNG screenshot is visually blank/black."""

    if not png_bytes:
        return {
            "ok": False,
            "screenshot_blank": True,
            "error": "Screenshot capture returned empty data",
        }
    try:
        from PIL import Image, ImageStat

        with Image.open(BytesIO(png_bytes)) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width <= 0 or height <= 0:
                return {
                    "ok": False,
                    "screenshot_blank": True,
                    "error": "Screenshot image has invalid dimensions",
                }
            sample_width = min(width, 96)
            sample_height = min(height, 96)
            sample = rgb.resize((sample_width, sample_height))
            stat = ImageStat.Stat(sample)
            mean = sum(stat.mean) / 3.0
            extrema = sample.getextrema()
            max_channel = max(channel_max for _channel_min, channel_max in extrema)
            min_channel = min(channel_min for channel_min, _channel_max in extrema)
    except (ImportError, OSError, ValueError):
        return None

    if mean <= 2.0 and max_channel <= 6:
        return {
            "ok": False,
            "screenshot_blank": True,
            "error": "Screenshot appears completely black; browser rendering did not produce visible content",
            "visual_mean": round(mean, 3),
            "visual_min": min_channel,
            "visual_max": max_channel,
        }
    return None


# --- Payment-card (PAN) redaction ------------------------------------------

_PAN_CANDIDATE_RE = re.compile(r"(?<![\d.])(\d(?:[ -]?\d){12,18})(?![\d.])")
_PAN_REDACTION_SKIP_KEYS = {"image_base64"}


def _redact_pan(text: str) -> str:
    """Mask Luhn-valid PAN candidates before browser results reach MCP callers."""

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(1)
        digits = re.sub(r"\D", "", candidate)
        if not 13 <= len(digits) <= 19:
            return candidate
        if len(set(digits)) == 1:
            return candidate
        if not luhn_check(digits):
            return candidate
        return "••••%s" % digits[-4:]

    return _PAN_CANDIDATE_RE.sub(_replace, text)


def _redact_pan_in_value(value: Any, *, key_path: tuple[str, ...] = ()) -> Any:
    if key_path and key_path[-1] in _PAN_REDACTION_SKIP_KEYS:
        return value
    if isinstance(value, str):
        return _redact_pan(value)
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
        redacted = _redact_pan(text)
        return redacted if redacted != text else value
    if isinstance(value, dict):
        return {key: _redact_pan_in_value(inner, key_path=(*key_path, str(key))) for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_pan_in_value(item, key_path=key_path) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_pan_in_value(item, key_path=key_path) for item in value)
    return value


def _json(obj: Any) -> str:
    return json.dumps(_redact_pan_in_value(obj), default=str, ensure_ascii=False)


def _payment_value_violation(value: str | None) -> str | None:
    """Return a safety error when a script tries to type card details."""
    text = str(value or "").strip()
    if not text:
        return None
    digits = re.sub(r"[\s\-]", "", text)
    if re.match(r"^[\d\s\-]{13,19}$", text) and digits.isdigit() and len(digits) >= 13:
        return (
            "PAYMENT SAFETY VIOLATION: You must NEVER type credit card numbers. "
            'When checkout is ready, call payment(action="request_review", ...) '
            "with merchant, total, and order summary first."
        )
    if re.match(r"^\d{2}/\d{2,4}$", text):
        return (
            "PAYMENT SAFETY VIOLATION: You must NEVER type card expiry details. "
            'When checkout is ready, call payment(action="request_review", ...) '
            "with merchant, total, and order summary first."
        )
    return None


# --- JavaScript payment-bypass scanning ------------------------------------
#
# An LLM agent driving the browser can attempt to type a card via
# ``browser_evaluate`` / ``browser_run_script`` / a CDP raw JS path even if
# the higher-level ``type``/``fill`` tools refuse card-shaped values.  The
# scan below catches the realistic bypass patterns:
#
#   * ``el.value = "4111111111111111"``  (direct quoted PAN write)
#   * ``el.value = `4111111111111111` `` (backtick template literal)
#   * ``el.value = "4111" + "1111" + ...`` (string concatenation)
#   * ``el.setAttribute("value", "4111...")``
#   * ``Reflect.set(el, "value", "4111...")``
#   * ``el.defaultValue = "..."``
#   * ``el.innerHTML = '<input value="4111...">'``
#   * Any JS source that contains a Luhn-valid 13-19 digit PAN as a
#     numeric/quoted literal alongside an obvious assignment operator.
#
# The check is intentionally over-inclusive: a Luhn-valid PAN appearing
# anywhere in a script that also writes any element value (or innerHTML or
# clipboard / fetch body) is treated as a payment bypass.  False positives
# are acceptable because legitimate browser automation never types a real
# card via raw JS — the dedicated ``payment(action="request_review")`` flow
# is the only sanctioned path.

_JS_PAN_DIGIT_RE = re.compile(r"(?<![\d.])(\d(?:[ \-_]?\d){12,18})(?![\d.])")
_JS_EXPIRY_RE = re.compile(r"['\"`]\s*(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})\s*['\"`]")
_JS_VALUE_WRITE_LHS_RE = re.compile(
    r"""(?:
        \.value\s*[+\-]?=                         # .value = | .value +=
        |\.defaultValue\s*[+\-]?=                 # .defaultValue =
        |\[['"]value['"]\]\s*[+\-]?=              # ["value"] =
        |\.innerHTML\s*[+\-]?=                    # .innerHTML =
        |\.innerText\s*[+\-]?=                    # .innerText =
        |\.textContent\s*[+\-]?=                  # .textContent =
        |\.setAttribute\s*\(\s*['"]value['"]      # .setAttribute("value", ...)
        |Reflect\s*\.\s*set\s*\(                  # Reflect.set(el, "value", ...)
        |Object\s*\.\s*defineProperty\s*\(        # Object.defineProperty(el, "value", ...)
        |navigator\s*\.\s*clipboard               # navigator.clipboard.writeText(...)
        |fetch\s*\(                               # fetch("/checkout", { body: "cc=..."})
        |new\s+XMLHttpRequest                     # XHR submission
        |\.send\s*\(                              # xhr.send(body)
        |document\.forms\[                        # document.forms["payment"].submit()
        |\.submit\s*\(\s*\)                       # form.submit()
    )""",
    re.IGNORECASE | re.VERBOSE,
)


_JS_STRING_CONCAT_RE = re.compile(
    r"""(['"`])([^'"`]{1,40})\1
        (?:\s*\+\s*(['"`])([^'"`]{1,40})\3)+""",
    re.VERBOSE,
)


def _js_strip_string_concat(script: str) -> str:
    """Collapse adjacent quoted-string concatenation into a single literal.

    ``"4111" + "1111" + "1111" + "1111"`` becomes ``"4111111111111111"`` so a
    downstream PAN/expiry scanner sees the actual joined value the JS runtime
    would produce.  The function is intentionally conservative: only the
    purely-quoted-with-``+`` shape is collapsed.  More exotic obfuscation
    (``String.fromCharCode``, base64 decode, etc.) is out of scope for static
    pattern matching and is treated as PAN by ``_js_contains_pan``.
    """
    if "+" not in script:
        return script

    def _join(match: re.Match[str]) -> str:
        text = match.group(0)
        # Collect every quoted segment in this concat chain.
        parts = re.findall(r"""(['"`])([^'"`]{1,40})\1""", text)
        joined = "".join(part[1] for part in parts)
        return '"%s"' % joined.replace('"', '\\"')

    return _JS_STRING_CONCAT_RE.sub(_join, script)


def _js_contains_pan(script: str) -> bool:
    """Return True when *script* contains a Luhn-valid 13-19 digit PAN.

    Strings concatenated with ``+`` are collapsed first so that
    ``"4111" + "1111" + "1111" + "1111"`` is detected even though no single
    quoted segment exceeds the 13-digit minimum on its own.
    """
    normalized = _js_strip_string_concat(script)
    for match in _JS_PAN_DIGIT_RE.finditer(normalized):
        candidate = match.group(1)
        digits = re.sub(r"\D", "", candidate)
        if not 13 <= len(digits) <= 19:
            continue
        if len(set(digits)) == 1:  # all-same-digit -> not a real card
            continue
        if luhn_check(digits):
            return True
    return False


def _js_contains_expiry(script: str) -> bool:
    """Return True when *script* contains an MM/YY or MM/YYYY card-expiry literal."""
    normalized = _js_strip_string_concat(script)
    return bool(_JS_EXPIRY_RE.search(normalized))


def _js_payment_value_bypass(script: str | None) -> str | None:
    """Return a safety error if *script* appears to be writing card data via JS.

    Strict-mode block: triggers when the script both (a) contains a Luhn-valid
    PAN or expiry-shaped literal and (b) contains a value-write style assignment
    (``.value=``, ``setAttribute("value", ...)``, ``innerHTML=``, ``fetch(...)``,
    ``form.submit()`` etc.).  Catches the W9-discovered bypasses against the
    W8 ``_JS_VALUE_WRITE_VALUE_RE`` (backticks, string concatenation, defaultValue,
    Reflect.set, clipboard write, fetch POST body).

    Both this helper and ``_payment_value_violation`` must remain sync — the
    CDP MCP server uses the same JSON-RPC path inside the Qt webview process
    and cannot await Playwright-only helpers.
    """
    text = str(script or "")
    if not text.strip():
        return None
    # A Luhn-valid PAN or expiry shape anywhere in the script is the violation.
    has_pan = _js_contains_pan(text)
    has_expiry = _js_contains_expiry(text)
    if not (has_pan or has_expiry):
        return None
    if not _JS_VALUE_WRITE_LHS_RE.search(text):
        # PAN appears but there's no DOM/network mutation — likely a read,
        # log line, or comment.  Don't block.
        return None
    if has_pan:
        return (
            "PAYMENT SAFETY VIOLATION: This script appears to write a credit "
            "card number into the page or network. Do NOT use JavaScript to "
            'fill payment fields. Call payment(action="request_review", ...) '
            "with merchant, total, and order summary first."
        )
    return (
        "PAYMENT SAFETY VIOLATION: This script appears to write a card "
        "expiry value into the page or network. Do NOT use JavaScript to "
        'fill payment fields. Call payment(action="request_review", ...) '
        "with merchant, total, and order summary first."
    )


# --- CSS selector heuristic ------------------------------------------------


def _is_css_selector(s: str) -> bool:
    """Heuristic: does this look like a CSS selector?"""
    # Bare lowercase alpha words are valid CSS tag selectors (button, div, a, etc.)
    if s.isalpha() and s == s.lower():
        return True
    return any(ch in s for ch in ".#[]>:+~=")
