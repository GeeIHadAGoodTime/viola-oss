"""Anonymized diagnostic-minimum builder.

This module builds the structured, allowlist-only diagnostic payload that (per
the founder's model) may ride EVERY bug report and crash by default, because it
is private *by construction*: it is assembled from a frozen allowlist of
non-identifying fields, so user identifiers cannot be present. It is the
counterpart to the opt-in "richer data" layer (contact info, verbatim body,
screenshots, cross-report identifiers) which stays consent-gated.

Design canon (why this is safe to send without opt-in):

1. ALLOWLIST, NOT DENYLIST. The payload is built from ``_ALLOWED_TOP_LEVEL_KEYS``
   only; the app-state slice is filtered against ``APP_STATE_FIELD_SPEC`` (an
   explicit key -> coercer table). A field that is not on the allowlist cannot
   reach the payload, so we never rely on "did the scrubber catch it?" for the
   structural shape.

2. STRUCTURED STACK CAPTURE, NEVER ``traceback.format_exc``. A formatted
   traceback string carries absolute file paths (``C:\\Users\\<name>\\...`` leaks
   the OS username), and with some tooling, local-variable ``repr`` (leaks user
   input / memory). We walk ``traceback.extract_tb`` and keep only
   ``{module, function, lineno}`` per frame -- code identifiers and integers,
   never a filesystem path, never a source line, never a local.

3. THE ONE SCRUBBER-LOAD-BEARING FIELD is the exception *value* (``str(exc)``),
   e.g. ``KeyError: 'jane@example.com'``. The type is always safe; the value can
   echo user input, so it is scrubbed through
   ``intent.log_redaction.redact_diagnostic_payload`` AND length-bounded. The
   ratchet gate fuzzes this field hard.

4. DEFENSE-IN-DEPTH SECOND PASS. After assembly the whole dict is run through
   ``redact_diagnostic_payload`` again. If the structural allowlist is ever
   wrong, this is the backstop -- but it is NOT the primary guarantee.

NEVER captured here: raw memory dumps, ``os.environ``, ``sys.executable``,
``platform.node()`` / hostname, ``getpass.getuser()``, local variables, request
bodies, verbatim user content, contact info, or any stable per-install id. Those
belong (if at all) to the opt-in layer, which is a separate module and a
separate consent.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import traceback
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1

# This builder runs inside a crash handler, so it must NEVER raise. We catch the
# realistic failure set from str()/platform/import/regex operations and fail
# closed. Mirrors the _SANITIZER_EXCEPTIONS idiom in intent.log_redaction and
# core.sentry_pii_filter (a named tuple, not a blind ``except Exception``).
_SAFE_CAPTURE_EXCEPTIONS = (
    ArithmeticError,
    AttributeError,
    ImportError,
    LookupError,
    OSError,
    RecursionError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
    re.error,
)

# User-directory path scrubbing. redact_diagnostic_payload handles secrets, card
# data, email/phone/SSN/IP -- but NOT the OS username embedded in a filesystem
# path (that regex lives in core.sentry_pii_filter, which operates on whole Sentry
# events). Since the exception message and module label are the fields most likely
# to carry an absolute path, we strip the username segment here as a first pass.
_WINDOWS_USER_PATH_RE = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/:\s]+")
_POSIX_USER_PATH_RE = re.compile(r"(?i)(/(?:home|Users)/)[^/\s]+")

# General absolute-path scrubbing. redact_diagnostic_payload covers secrets, card
# data, email/phone/SSN/IP -- but NOT a filesystem path off the user profile,
# which leaks the drive/folder/file names the user chose, a UNC hostname, and
# (even under C:\Users\<name>) the filename after the username. A crash message is
# frequently just "cannot open <absolute path>", so ANY absolute path is collapsed
# to one opaque token here; the structured stack (module/function/lineno) still
# localizes the crash without a path. Order in _scrub_text: neutralize whole
# absolute paths FIRST, so the narrower username regexes above only ever see
# residue.
_ABS_PATH_RES = (
    # Windows drive path: C:\... or C:/...  The (?<![A-Za-z]) guard keeps the "p:/"
    # inside "http://" from matching as a drive path.
    re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|]*"),
    # UNC path: \\server\share\...  (also leaks the hostname).
    re.compile(r"\\\\[^\s\"'<>|]+"),
    # POSIX absolute path under a known root dir. The (?<![\w:]) guard avoids
    # matching a URL path segment (preceded by a word char / ':') or an inline
    # fraction like "and/or"; the root-dir allowlist avoids eating ordinary prose
    # slashes while still covering the real desktop mount roots.
    re.compile(
        r"(?<![\w:])/(?:home|users|root|var|tmp|temp|opt|mnt|media|srv|etc|"
        r"private|volumes|applications|library|data|usr)(?:/[^\s\"'<>|]*)?",
        re.IGNORECASE,
    ),
)

# Reports we attach the minimum to. "crash" = automatic (unhandled exception /
# faulthandler); "bug_report" = user-initiated.
_ALLOWED_REPORT_KINDS = frozenset({"crash", "bug_report"})

# The complete set of keys allowed at the top level of the minimum payload.
# Nothing outside this set is ever emitted. Keep in sync with the gate.
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "report_kind",
        "app_version",
        "surface",
        "os_family",
        "os_version",
        "arch",
        "python_version",
        "error_type",
        "error_value",
        "error_location",
        "stack",
        "app_state",
    }
)

# Keys allowed inside error_location.
_ALLOWED_LOCATION_KEYS = frozenset({"module", "function", "lineno"})

# Surface label for an error raised in the desktop's React UI (as opposed to
# "desktop_qt", the Python process hosting it).
_BROWSER_SURFACE = "desktop_react"

# A browser stack frame's filename is a URL. Only a plain asset basename is
# allowed through as the module label -- see _browser_module_from_url for why
# this is an allow-shape rather than a strip-shape.
_BROWSER_MODULE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

# Keys allowed inside each stack frame.
_ALLOWED_FRAME_KEYS = frozenset({"module", "function", "lineno"})

_MAX_ERROR_VALUE_LEN = 300
_MAX_STACK_FRAMES = 30
_MAX_MODULE_LEN = 200
_MAX_FUNCTION_LEN = 120
_MAX_OS_VERSION_LEN = 60
_MAX_SURFACE_LEN = 40

# App-state allowlist: only these keys may enter the payload, each coerced to a
# safe non-identifying shape. Values are ALWAYS enum-like strings, bools, or
# bounded ints -- NEVER free text and NEVER user content. Callers hand us a raw
# snapshot; we take only what is on this list. Extend deliberately (and the gate
# will require the extension to keep a coercer, never a raw passthrough).
APP_STATE_FIELD_SPEC: dict[str, str] = {
    # coercer name per key: "enum" (bounded token), "bool", "int"
    "surface": "enum",
    "ui_view": "enum",
    "display_mode": "enum",
    "stage_mode": "enum",
    "ai_source": "enum",
    "llm_provider": "enum",
    "music_provider": "enum",
    "is_authenticated": "bool",
    "is_agent_running": "bool",
    "active_task_kind": "enum",
    "execution_stage": "enum",
    "wake_active": "bool",
    "audio_role": "enum",
    "network_online": "bool",
    "recent_step_kind": "enum",
    "recent_tool_name": "enum",
    "severity": "enum",
    "step_count": "int",
    "retry_count": "int",
}

# Enum-token values are bounded and stripped to a conservative character set so a
# mislabeled free-text value cannot smuggle PII through an "enum" slot.
_MAX_ENUM_LEN = 64
_ENUM_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
_MAX_INT_ABS = 10_000_000


def build_diagnostic_minimum(
    *,
    report_kind: str,
    exc: BaseException | None = None,
    exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
    app_state: Mapping[str, Any] | None = None,
    app_version: str | None = None,
    surface: str | None = None,
    os_info: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the anonymized diagnostic-minimum payload.

    Args:
        report_kind: "crash" or "bug_report".
        exc: The exception to summarize (crash path). Optional for bug reports.
        exc_info: Optional ``(type, value, tb)`` triple; if given, its traceback
            is used for the structured stack.
        app_state: Raw runtime snapshot; only allowlisted keys are kept.
        app_version: Overrides the detected app version (else VIOLA_VERSION).
        surface: "desktop_qt" | "cloud" | "website" | ... (bounded enum).
        os_info: Optional injected ``{"os_family","os_version","arch"}`` for
            tests / deterministic capture; else detected without host identity.

    The result contains only keys in ``_ALLOWED_TOP_LEVEL_KEYS`` and is safe to
    transmit without user opt-in.
    """
    kind = report_kind if report_kind in _ALLOWED_REPORT_KINDS else "bug_report"

    resolved_exc = exc
    tb = None
    if exc_info is not None and isinstance(exc_info, tuple) and len(exc_info) == 3:
        resolved_exc = resolved_exc or exc_info[1]
        tb = exc_info[2]
    elif resolved_exc is not None:
        tb = resolved_exc.__traceback__

    os_slice = _os_slice(os_info)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": kind,
        "app_version": _safe_enum(app_version) or _detect_app_version(),
        "surface": _safe_enum(surface, limit=_MAX_SURFACE_LEN) or "unknown",
        "os_family": os_slice["os_family"],
        "os_version": os_slice["os_version"],
        "arch": os_slice["arch"],
        "python_version": _python_version(),
        "app_state": _allowlist_app_state(app_state),
    }

    if resolved_exc is not None:
        payload["error_type"] = _safe_enum(type(resolved_exc).__name__, limit=_MAX_FUNCTION_LEN) or "Exception"
        payload["error_value"] = _safe_error_value(resolved_exc)
        stack = _structured_stack(tb)
        payload["stack"] = stack
        payload["error_location"] = stack[-1] if stack else {}

    # Defense-in-depth: a second scrub over the fully-assembled payload. The
    # allowlist above is the primary guarantee; this is the backstop.
    scrubbed = _defense_in_depth_scrub(payload)

    # Structural belt-and-suspenders: drop anything that somehow escaped the
    # allowlist during scrubbing (the scrubber must never ADD keys, but we assert
    # it here so the contract is enforced at runtime, not just in the gate).
    return {key: value for key, value in scrubbed.items() if key in _ALLOWED_TOP_LEVEL_KEYS}


def build_browser_diagnostic_minimum(
    *,
    error_type: Any,
    error_value: Any,
    frames: Any = None,
    app_state: Mapping[str, Any] | None = None,
    app_version: str | None = None,
    surface: str | None = None,
    os_info: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the anonymized minimum for an error raised in the DESKTOP UI.

    Same output contract as :func:`build_diagnostic_minimum` -- only keys in
    ``_ALLOWED_TOP_LEVEL_KEYS``, same scrubber on the error value, same bounded
    ``{module, function, lineno}`` frames -- but sourced from a browser error
    instead of a Python exception. It lives beside its Python sibling on
    purpose: the module docstring's four rules are the safety contract, and a
    second sanitizer in a second file is how the two drift apart.

    The caller is a NETWORK boundary (the desktop UI posts here over HTTP), so
    every field is treated as hostile input and rebuilt from scratch. Nothing
    the client sends is forwarded verbatim.

    Args:
        error_type: JS error constructor name, e.g. ``"TypeError"``.
        error_value: The error message. Scrubbed + bounded; this is the one
            field where the scrubber is load-bearing.
        frames: Sequence of ``{filename, function, lineno}`` mappings, oldest
            frame first (the Sentry SDK's ordering), so the crash site is last.
        app_state: Raw UI snapshot; only ``APP_STATE_FIELD_SPEC`` keys survive.
        app_version / surface / os_info: as per the Python builder.
    """
    os_slice = _os_slice(os_info)
    stack = _browser_structured_stack(frames)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "crash",
        "app_version": _safe_enum(app_version) or _detect_app_version(),
        "surface": _safe_enum(surface, limit=_MAX_SURFACE_LEN) or _BROWSER_SURFACE,
        "os_family": os_slice["os_family"],
        "os_version": os_slice["os_version"],
        "arch": os_slice["arch"],
        "python_version": _python_version(),
        "error_type": _safe_enum(error_type, limit=_MAX_FUNCTION_LEN) or "Error",
        "error_value": _safe_error_text(error_value),
        "stack": stack,
        "error_location": stack[-1] if stack else {},
        "app_state": _allowlist_app_state(app_state),
    }

    scrubbed = _defense_in_depth_scrub(payload)
    return {key: value for key, value in scrubbed.items() if key in _ALLOWED_TOP_LEVEL_KEYS}


def _browser_structured_stack(frames: Any) -> list[dict[str, Any]]:
    """Rebuild a browser stack as bounded ``{module, function, lineno}`` frames.

    Never emits a URL, a query string, a column, or a source line -- only the
    asset BASENAME, the function identifier, and the line number.
    """
    if not isinstance(frames, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for frame in frames[-_MAX_STACK_FRAMES:]:
        if not isinstance(frame, Mapping):
            continue
        lineno = frame.get("lineno")
        out.append(
            {
                "module": _browser_module_from_url(frame.get("filename")),
                "function": _safe_enum(frame.get("function"), limit=_MAX_FUNCTION_LEN) or "<unknown>",
                "lineno": _safe_int(lineno) if isinstance(lineno, (int, float, str)) else None,
            }
        )
    return out


def _browser_module_from_url(raw: Any) -> str:
    """Reduce a script URL to a non-identifying asset label.

    A browser frame's ``filename`` is a URL, and a URL is a richer leak than a
    Python path: it can carry a query string, a ``file:///C:/Users/<name>/``
    prefix, or a ``data:`` URI whose body is arbitrary (possibly user) content.
    So this is an ALLOW-shape, not a strip-shape: query and fragment go first,
    then only the last path segment survives, and only if it looks like an
    ordinary asset filename. Anything else collapses to ``<unknown>`` rather
    than being cleaned up and kept.
    """
    try:
        text = str(raw or "")
    except _SAFE_CAPTURE_EXCEPTIONS:
        return "<unknown>"
    if not text:
        return "<unknown>"
    normalized = text.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    basename = normalized.rsplit("/", 1)[-1].strip()
    if not basename or not _BROWSER_MODULE_RE.match(basename):
        return "<unknown>"
    return _scrub_text(basename)[:_MAX_MODULE_LEN] or "<unknown>"


def _detect_app_version() -> str:
    try:
        from core.constants import VIOLA_VERSION

        return _safe_enum(VIOLA_VERSION) or "unknown"
    except _SAFE_CAPTURE_EXCEPTIONS:
        return "unknown"


def _python_version() -> str:
    info = sys.version_info
    return "%d.%d.%d" % (info.major, info.minor, info.micro)


def _os_slice(os_info: Mapping[str, str] | None) -> dict[str, str]:
    """Coarse OS descriptors with NO host identity.

    Uses ``platform.system``/``platform.release`` and ``platform.machine`` only.
    Deliberately never touches ``platform.node`` (hostname), ``os.environ``,
    ``socket.gethostname``, or ``getpass.getuser``.
    """
    if os_info is not None:
        return {
            "os_family": _safe_enum(os_info.get("os_family")) or "unknown",
            "os_version": _safe_enum(os_info.get("os_version"), limit=_MAX_OS_VERSION_LEN) or "unknown",
            "arch": _safe_enum(os_info.get("arch")) or "unknown",
        }
    try:
        family = platform.system() or "unknown"
    except _SAFE_CAPTURE_EXCEPTIONS:
        family = "unknown"
    try:
        # release() is a coarse OS version ("10", "23.4.0"); it is not a hostname.
        version = platform.release() or "unknown"
    except _SAFE_CAPTURE_EXCEPTIONS:
        version = "unknown"
    try:
        arch = platform.machine() or "unknown"
    except _SAFE_CAPTURE_EXCEPTIONS:
        arch = "unknown"
    return {
        "os_family": _safe_enum(family) or "unknown",
        "os_version": _safe_enum(version, limit=_MAX_OS_VERSION_LEN) or "unknown",
        "arch": _safe_enum(arch) or "unknown",
    }


def _structured_stack(tb: Any) -> list[dict[str, Any]]:
    """Extract a scrubbed structured stack: module/function/lineno per frame.

    NEVER emits the absolute filename or the source line. The filename is reduced
    to a repo-relative dotted-ish module when it lives under the project root,
    else to its basename (which cannot contain a directory username), and then
    scrubbed as a final guard.
    """
    if tb is None:
        return []
    try:
        frames = traceback.extract_tb(tb, limit=_MAX_STACK_FRAMES)
    except _SAFE_CAPTURE_EXCEPTIONS:
        return []

    project_root = _project_root_str()
    out: list[dict[str, Any]] = []
    for frame in frames:
        module = _module_from_filename(getattr(frame, "filename", ""), project_root)
        function = _safe_enum(getattr(frame, "name", ""), limit=_MAX_FUNCTION_LEN) or "<unknown>"
        lineno = getattr(frame, "lineno", None)
        out.append(
            {
                "module": module,
                "function": function,
                "lineno": int(lineno) if isinstance(lineno, int) else None,
            }
        )
    return out


def _project_root_str() -> str:
    try:
        from core.platform import get_project_root

        return str(get_project_root()).replace("\\", "/").rstrip("/")
    except _SAFE_CAPTURE_EXCEPTIONS:
        return ""


def _module_from_filename(filename: Any, project_root: str) -> str:
    """Reduce an absolute source filename to a non-identifying module label."""
    try:
        text = str(filename or "")
    except _SAFE_CAPTURE_EXCEPTIONS:
        return "<unknown>"
    if not text:
        return "<unknown>"
    normalized = text.replace("\\", "/")
    label = ""
    if project_root and normalized.lower().startswith(project_root.lower() + "/"):
        rel = normalized[len(project_root) + 1 :]
        label = rel.rsplit(".py", 1)[0].strip("/").replace("/", ".")
    if not label:
        # Basename only -- a bare filename cannot carry a directory username.
        label = os.path.basename(normalized) or "<unknown>"
    # Final guard: scrub (removes any residual user-path/email/etc.) and bound.
    label = _scrub_text(label)[:_MAX_MODULE_LEN]
    return label or "<unknown>"


def _safe_error_value(exc: BaseException) -> str:
    """Scrubbed + bounded ``str(exc)``.

    This is the single field where the scrubber is load-bearing, because the
    exception message can echo user input or a filesystem path.
    """
    try:
        raw = str(exc)
    except _SAFE_CAPTURE_EXCEPTIONS:
        return "<unrepresentable>"
    return _safe_error_text(raw)


def _safe_error_text(raw: Any) -> str:
    """Scrubbed + bounded free text for the one scrubber-load-bearing field.

    Split out of :func:`_safe_error_value` so the browser builder applies the
    IDENTICAL treatment to a JavaScript error message, which is every bit as
    likely to echo what the user typed.
    """
    try:
        text = str(raw or "")
    except _SAFE_CAPTURE_EXCEPTIONS:
        return "<unrepresentable>"
    scrubbed = _scrub_text(text)
    if len(scrubbed) > _MAX_ERROR_VALUE_LEN:
        scrubbed = scrubbed[: _MAX_ERROR_VALUE_LEN - 3] + "..."
    return scrubbed


def _allowlist_app_state(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, coercer in APP_STATE_FIELD_SPEC.items():
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if coercer == "bool":
            out[key] = bool(value)
        elif coercer == "int":
            coerced_int = _safe_int(value)
            if coerced_int is not None:
                out[key] = coerced_int
        else:  # "enum"
            token = _safe_enum(value)
            if token:
                out[key] = token
    return out


def _safe_enum(value: Any, *, limit: int = _MAX_ENUM_LEN) -> str:
    """Coerce to a bounded enum-token: strip to a conservative charset.

    An "enum" slot must never carry PII. We keep only a conservative character
    set (identifiers, dots, colons, hyphens) and bound the length, so a
    mislabeled free-text value collapses to a harmless token or "".
    """
    if value is None:
        return ""
    try:
        text = str(value).strip()
    except _SAFE_CAPTURE_EXCEPTIONS:
        return ""
    if not text:
        return ""
    # Scrub BEFORE charset-filtering: a phone/SSN/card is all-allowed-chars, so it
    # would survive the charset filter intact. Scrubbing first collapses it to a
    # [REDACTED:*] marker while the original separators still let the regex match.
    text = _scrub_text(text)
    filtered = "".join(char for char in text if char in _ENUM_ALLOWED)
    return filtered[:limit]


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < -_MAX_INT_ABS:
        return -_MAX_INT_ABS
    if parsed > _MAX_INT_ABS:
        return _MAX_INT_ABS
    return parsed


def _scrub_text(text: str) -> str:
    """Run text through the shared diagnostic scrubber (fail-closed to redacted).

    Strips the OS-username path segment first (not covered by
    redact_diagnostic_payload), then applies the shared secret/card/PII scrubber.
    """
    try:
        # Whole absolute paths first (drive-letter, UNC, POSIX-under-known-root),
        # so nothing after a redacted username segment survives either.
        stripped = text
        for _abs_path_re in _ABS_PATH_RES:
            stripped = _abs_path_re.sub("[REDACTED:PATH]", stripped)
        stripped = _WINDOWS_USER_PATH_RE.sub(r"\1[REDACTED:USER]", stripped)
        stripped = _POSIX_USER_PATH_RE.sub(r"\1[REDACTED:USER]", stripped)
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(stripped)
        return redacted if isinstance(redacted, str) else str(redacted)
    except _SAFE_CAPTURE_EXCEPTIONS:
        # Fail closed: if the scrubber is unavailable we cannot prove the text is
        # clean, so redact it wholesale rather than risk a leak.
        return "[REDACTED:SCRUBBER_UNAVAILABLE]"


def _defense_in_depth_scrub(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(payload)
    except _SAFE_CAPTURE_EXCEPTIONS:
        return payload
    return redacted if isinstance(redacted, dict) else payload


__all__ = [
    "APP_STATE_FIELD_SPEC",
    "SCHEMA_VERSION",
    "build_browser_diagnostic_minimum",
    "build_diagnostic_minimum",
]
