"""Pure proof-evaluation helpers for the live voice oracle."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_TERM_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_FAILURE_RE = re.compile(
    r"\b(?:fail|failed|failure)\b" r"(?![\"']?\s*[:=]\s*(?:false|null|none|0|\"\"|\[\]|\{\}))",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(
    r"\berror\b" r"(?![\"']?\s*[:=]\s*(?:false|null|none|0|\"\"|\[\]|\{\}))",
    re.IGNORECASE,
)
_IS_ERROR_TRUE_RE = re.compile(r"\bis_error[\"']?\s*[:=]\s*true\b", re.IGNORECASE)

NEGATIVE_EVIDENCE_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "quota_exceeded",
    "quota exceeded",
    "quota",
    "429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "rate-limit",
    "ratelimiterror",
    "provider failure",
    "provider failed",
    "provider error",
    "openai responses request failed",
    "couldn't complete",
    "could not complete",
    "llm_error",
    "tool_error",
    "model-load",
    "model load",
    "model loaded",
    "loading model",
    "missing model",
    "failed to load",
    "model failure",
    "model error",
    "onnxruntimeerror",
)


def expected_command_terms(command_text: str, supplied_terms: Sequence[str] | None = None) -> dict[str, Any]:
    """Return the full command terms and whether supplied terms are complete."""
    derived_terms = _unique_terms(command_text)
    explicit_terms = _unique_terms(" ".join(supplied_terms or ())) if supplied_terms is not None else []
    if not derived_terms:
        return {
            "ok": False,
            "reason": "command_text_empty",
            "command_text": command_text,
            "terms": [],
            "derived_terms": [],
            "explicit_terms": explicit_terms,
            "missing_terms": [],
        }
    if supplied_terms is None:
        return {
            "ok": True,
            "reason": "derived_from_command_text",
            "command_text": command_text,
            "terms": derived_terms,
            "derived_terms": derived_terms,
            "explicit_terms": [],
            "missing_terms": [],
        }
    if not explicit_terms:
        return {
            "ok": False,
            "reason": "expected_terms_empty",
            "command_text": command_text,
            "terms": derived_terms,
            "derived_terms": derived_terms,
            "explicit_terms": explicit_terms,
            "missing_terms": derived_terms,
        }
    missing_terms = [term for term in derived_terms if term not in set(explicit_terms)]
    return {
        "ok": not missing_terms,
        "reason": "explicit_terms_cover_command" if not missing_terms else "expected_terms_partial",
        "command_text": command_text,
        "terms": derived_terms,
        "derived_terms": derived_terms,
        "explicit_terms": explicit_terms,
        "missing_terms": missing_terms,
    }


def evaluate_positive_log_lines(text: str, positive_patterns: Sequence[str]) -> dict[str, Any]:
    """Evaluate positive INTENT/TTS-style log lines without trusting poisoned lines."""
    accepted_lines: list[str] = []
    rejected_lines: list[dict[str, Any]] = []
    matched_lines: list[dict[str, Any]] = []
    for line in text.splitlines():
        matched_patterns = _matched_patterns(line, positive_patterns)
        if not matched_patterns:
            continue
        markers = negative_evidence_markers(line)
        line_result = {"line": line.strip(), "patterns": matched_patterns, "markers": markers}
        matched_lines.append(line_result)
        if markers:
            rejected_lines.append(line_result)
        else:
            accepted_lines.append(line.strip())
    return {
        "accepted": bool(accepted_lines),
        "reason": "positive_lines_accepted" if accepted_lines else "no_clean_positive_lines",
        "accepted_lines": accepted_lines,
        "rejected_lines": rejected_lines,
        "matched_lines": matched_lines,
        "positive_patterns": list(positive_patterns),
    }


def evaluate_trace_candidate(
    *,
    command_text: str,
    trace_text: str,
    index_outcome: str | None,
    candidate_mtime: float,
    run_started_at: float,
    run_finished_at: float,
    supplied_terms: Sequence[str] | None = None,
    candidate_path: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate whether one TraceReader candidate proves this oracle command succeeded."""
    terms_result = expected_command_terms(command_text, supplied_terms)
    trace_terms = set(_unique_terms(trace_text))
    command_phrase = _normalized_phrase(command_text)
    trace_phrase = _normalized_phrase(trace_text)
    full_command_seen = bool(command_phrase and command_phrase in trace_phrase)
    missing_trace_terms = [term for term in terms_result["terms"] if term not in trace_terms]
    all_terms_seen = bool(terms_result["terms"]) and not missing_trace_terms
    command_seen = bool(terms_result["ok"] and (full_command_seen or all_terms_seen))
    markers = negative_evidence_markers(trace_text)
    mtime_in_window = run_started_at <= candidate_mtime <= run_finished_at
    outcome_success = index_outcome == "success"
    checks = {
        "mtime_in_window": mtime_in_window,
        "expected_terms_ok": bool(terms_result["ok"]),
        "command_seen": command_seen,
        "outcome_success": outcome_success,
        "provider_failure_free": not markers,
    }
    accepted = all(checks.values())
    return {
        "accepted": accepted,
        "reason": "accepted" if accepted else _trace_rejection_reason(checks, terms_result),
        "candidate_path": str(candidate_path) if candidate_path is not None else None,
        "candidate_mtime": candidate_mtime,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "index_outcome": index_outcome,
        "checks": checks,
        "command_text": command_text,
        "terms": terms_result,
        "full_command_seen": full_command_seen,
        "all_terms_seen": all_terms_seen,
        "missing_trace_terms": missing_trace_terms,
        "provider_failure_seen": bool(markers),
        "provider_failure_markers": markers,
    }


def negative_evidence_markers(text: str) -> list[str]:
    """Return failure/quota/rate-limit/model-load markers present in text."""
    lowered = text.casefold()
    markers: list[str] = []
    for marker in NEGATIVE_EVIDENCE_MARKERS:
        if marker.casefold() in lowered and marker not in markers:
            markers.append(marker)
    for marker, pattern in (
        ("failure", _FAILURE_RE),
        ("error", _ERROR_RE),
        ("is_error=true", _IS_ERROR_TRUE_RE),
    ):
        if pattern.search(text) and marker not in markers:
            markers.append(marker)
    return markers


def _trace_rejection_reason(checks: dict[str, bool], terms_result: dict[str, Any]) -> str:
    if not checks["mtime_in_window"]:
        return "candidate_mtime_outside_run_window"
    if not checks["expected_terms_ok"]:
        return str(terms_result["reason"])
    if not checks["command_seen"]:
        return "command_not_seen"
    if not checks["outcome_success"]:
        return "trace_outcome_not_success"
    if not checks["provider_failure_free"]:
        return "provider_failure_seen"
    return "trace_candidate_rejected"


def _matched_patterns(line: str, positive_patterns: Sequence[str]) -> list[str]:
    lowered = line.casefold()
    return [pattern for pattern in positive_patterns if pattern.casefold() in lowered]


def _normalized_phrase(text: str) -> str:
    return " ".join(_TERM_RE.findall(text.casefold()))


def _unique_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in _TERM_RE.findall(text.casefold()):
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms
