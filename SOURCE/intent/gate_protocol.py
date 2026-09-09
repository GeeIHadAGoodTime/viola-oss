"""Helpers for Viola's user-facing gate messages."""

from __future__ import annotations

PAYMENT_GATE_PREFIX = "PAYMENT_GATE:"
SIGNATURE_GATE_PREFIX = "SIGNATURE_GATE:"

_GATE_PREFIXES_BY_KIND = {
    "payment": PAYMENT_GATE_PREFIX,
    "signature": SIGNATURE_GATE_PREFIX,
}


def _canonical_gate_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized.endswith("_gate"):
        normalized = normalized[: -len("_gate")]
    if normalized not in _GATE_PREFIXES_BY_KIND:
        raise ValueError("unknown gate kind: %s" % kind)
    return normalized


def gate_prefix_for_kind(kind: str) -> str:
    return _GATE_PREFIXES_BY_KIND[_canonical_gate_kind(kind)]


def format_gate_message(text: str | None, kind: str) -> str:
    """Format a gate message produced by an explicit review-tool call."""

    stripped = str(text or "").strip()
    if not stripped:
        return ""
    prefix = gate_prefix_for_kind(kind)
    if stripped.startswith(prefix):
        return stripped
    return "%s %s" % (prefix, stripped)


def gate_message_body(text: str | None, kind: str) -> str:
    """Return the body of a gate message produced by an explicit review tool."""

    stripped = str(text or "").strip()
    prefix = gate_prefix_for_kind(kind)
    if stripped.startswith(prefix):
        return stripped[len(prefix) :].strip()
    return stripped
