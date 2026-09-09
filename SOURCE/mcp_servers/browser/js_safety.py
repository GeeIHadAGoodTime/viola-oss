"""Shared browser JavaScript safety checks."""

from __future__ import annotations

import re
import unicodedata

BLOCKED_JS_PATTERNS = (
    "document.cookie",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "navigator.credentials",
    "XMLHttpRequest",
    "fetch(",
    "WebSocket(",
    "ServiceWorker",
    "importScripts",
    "new Function",
    "__proto__",
    "constructor['constructor",
    'constructor["constructor',
    "fromCharCode",
    "import(",
    "globalThis[",
    "window['eval",
    'window["eval',
    "window['Function",
    'window["Function',
    "atob(",
    "\\x",
    "\\u00",
    "eval(",
    "eval (",
    "Function(",
    "Function (",
    "Proxy(",
    "Proxy.",
    "Reflect.",
    "fromCodePoint",
    "import (",
    "['cookie']",
    '["cookie"]',
    "['localStorage']",
    '["localStorage"]',
    "['sessionStorage']",
    '["sessionStorage"]',
    "navigator.sendBeacon",
    "navigator.clipboard",
    "window.open",
    "SharedWorker",
)

BRACKET_ACCESS_RES = tuple(
    re.compile(p)
    for p in (
        r"""\[\s*['"]cookie['"]\s*\]""",
        r"""\[\s*['"]localStorage['"]\s*\]""",
        r"""\[\s*['"]sessionStorage['"]\s*\]""",
    )
)

OBFUSCATION_CONCAT_RE = re.compile(
    r"""(?:['"][a-zA-Z]{2,}['"]\s*\+\s*['"][a-zA-Z]{2,}['"])""",
)
CONCAT_TARGET_WORDS = (
    "document",
    "cookie",
    "localstorage",
    "sessionstorage",
    "indexeddb",
    "xmlhttprequest",
    "websocket",
    "serviceworker",
    "importscripts",
    "function",
    "globalthis",
    "navigator",
    "credentials",
    "eval",
)
CI_PATTERNS = (
    "document.cookie",
    "localstorage",
    "sessionstorage",
    "indexeddb",
    "navigator.credentials",
    "serviceworker",
    "importscripts",
    "globalthis",
    "fromcharcode",
    "fromcodepoint",
    "navigator.sendbeacon",
    "navigator.clipboard",
    "sharedworker",
    "window.open",
)


def blocked_js_pattern(script: str) -> str | None:
    """Return the blocked JavaScript pattern, or None when the script is allowed."""
    script_normalized = unicodedata.normalize("NFKC", script)

    for blocked in BLOCKED_JS_PATTERNS:
        if blocked in script_normalized:
            return blocked

    for bracket_re in BRACKET_ACCESS_RES:
        if bracket_re.search(script_normalized):
            return "bracket:%s" % bracket_re.pattern

    script_lower = script_normalized.lower()
    for pattern in CI_PATTERNS:
        if pattern in script_lower:
            return pattern

    concat_matches = OBFUSCATION_CONCAT_RE.findall(script_normalized)
    for match in concat_matches:
        parts = re.split(r"""['"]\s*\+\s*['"]""", match)
        combined = "".join(part.strip("'\"") for part in parts).lower()
        for target in CONCAT_TARGET_WORDS:
            if target in combined:
                return "obfuscated:%s" % target

    return None
