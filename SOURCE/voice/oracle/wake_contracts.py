"""Pure wake-policy contract helpers for voice oracle evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

REQUIRED_WAKE_POLICY_LAYERS = (
    "listening_gate",
    "zero_input",
    "primary_score",
    "vad_gate",
    "cooldown",
)

NEGATIVE_AUDIO_INSPECTION_MARKERS = (
    "WAKE_AUDIO_DIAG",
    "[STAGE1] audio_in",
    "[STAGE2] score",
    "[STAGE5] policy",
)

_POLICY_LINE_RE = re.compile(
    r"\[STAGE5\]\s*policy:\s*(ALLOW|DENY)\b.*?passed=\[(?P<passed>.*?)\]\s*failed=\[(?P<failed>.*?)\]",
    re.IGNORECASE | re.DOTALL,
)
_APPROVED_SCORE_RE = re.compile(
    r"Wake trigger approved:\s*score\s*=\s*(?P<score>[0-9]+(?:\.[0-9]+)?)\s*,?\s*"
    r"threshold\s*=\s*(?P<threshold>[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_APPROVED_SCORE_LINE_RE = re.compile(r"Wake trigger approved:.*score\s*=", re.IGNORECASE)
_LAYER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_ACCEPT_MARKER_PATTERNS = (
    ("Wake accepted", re.compile(r"Wake accepted", re.IGNORECASE)),
    ("Wake trigger approved", re.compile(r"Wake trigger approved:", re.IGNORECASE)),
    ("VIOLA detected", re.compile(r"VIOLA detected!?", re.IGNORECASE)),
    ("[WAKE->CMD]", re.compile(r"\[WAKE->CMD\]", re.IGNORECASE)),
    ("ALLOW", re.compile(r"\bALLOW\b", re.IGNORECASE)),
    ("LISTENING", re.compile(r"\bLISTENING\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class WakePolicyEvaluation:
    allow_seen: bool
    approved_seen: bool
    passed_layers: tuple[str, ...]
    failed_layers: tuple[str, ...]
    missing_required_layers: tuple[str, ...]
    failed_required_layers: tuple[str, ...]
    score: float | None
    threshold: float | None
    threshold_crossed: bool
    threshold_requirement_passed: bool
    all_layers_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_seen": self.allow_seen,
            "approved_seen": self.approved_seen,
            "passed_layers": list(self.passed_layers),
            "failed_layers": list(self.failed_layers),
            "missing_required_layers": list(self.missing_required_layers),
            "missing_layers": list(self.missing_required_layers),
            "failed_required_layers": list(self.failed_required_layers),
            "score": self.score,
            "threshold": self.threshold,
            "threshold_crossed": self.threshold_crossed,
            "threshold_requirement_passed": self.threshold_requirement_passed,
            "all_layers_passed": self.all_layers_passed,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class NegativeAudioEvaluation:
    inspected: bool
    accepted: bool
    inspection_markers: tuple[str, ...]
    accept_markers: tuple[str, ...]
    failure_reasons: tuple[str, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "accepted": self.accepted,
            "inspection_markers": list(self.inspection_markers),
            "accept_markers": list(self.accept_markers),
            "failure_reasons": list(self.failure_reasons),
            "passed": self.passed,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def evaluate_wake_policy(
    text: str,
    *,
    required_layers: Iterable[str] = REQUIRED_WAKE_POLICY_LAYERS,
) -> WakePolicyEvaluation:
    required = tuple(required_layers)
    passed_layers: set[str] = set()
    failed_layers: set[str] = set()
    allow_seen = False

    for match in _POLICY_LINE_RE.finditer(text):
        if match.group(1).upper() == "ALLOW":
            allow_seen = True
        passed_layers.update(_parse_layers(match.group("passed")))
        failed_layers.update(_parse_layers(match.group("failed")))

    approved_seen = "Wake trigger approved:" in text
    score_pairs = [
        (float(match.group("score")), float(match.group("threshold"))) for match in _APPROVED_SCORE_RE.finditer(text)
    ]
    approved_score_line_count = len(_APPROVED_SCORE_LINE_RE.findall(text))
    score = score_pairs[-1][0] if score_pairs else None
    threshold = score_pairs[-1][1] if score_pairs else None
    threshold_crossed = bool(
        score_pairs and all(pair_score >= pair_threshold for pair_score, pair_threshold in score_pairs)
    )
    threshold_requirement_passed = approved_score_line_count == 0 or (
        approved_score_line_count == len(score_pairs) and threshold_crossed
    )

    missing_required_layers = tuple(layer for layer in required if layer not in passed_layers)
    failed_required_layers = tuple(layer for layer in required if layer in failed_layers)
    all_layers_passed = (
        (allow_seen or approved_seen)
        and not missing_required_layers
        and not failed_required_layers
        and threshold_requirement_passed
    )

    return WakePolicyEvaluation(
        allow_seen=allow_seen,
        approved_seen=approved_seen,
        passed_layers=tuple(sorted(passed_layers)),
        failed_layers=tuple(sorted(failed_layers)),
        missing_required_layers=missing_required_layers,
        failed_required_layers=failed_required_layers,
        score=score,
        threshold=threshold,
        threshold_crossed=threshold_crossed,
        threshold_requirement_passed=threshold_requirement_passed,
        all_layers_passed=all_layers_passed,
    )


def wake_policy_snapshot(
    text: str,
    *,
    required_layers: Iterable[str] = REQUIRED_WAKE_POLICY_LAYERS,
) -> WakePolicyEvaluation:
    return evaluate_wake_policy(text, required_layers=required_layers)


def evaluate_negative_audio(
    text: str,
    *,
    inspection_markers: Iterable[str] = NEGATIVE_AUDIO_INSPECTION_MARKERS,
) -> NegativeAudioEvaluation:
    inspection_marker_tuple = tuple(inspection_markers)
    found_inspection_markers = tuple(marker for marker in inspection_marker_tuple if marker in text)
    found_accept_markers = tuple(label for label, pattern in _ACCEPT_MARKER_PATTERNS if pattern.search(text))

    failure_reasons: list[str] = []
    if not found_inspection_markers:
        failure_reasons.append("no_inspection_markers")
    if found_accept_markers:
        failure_reasons.append("accept_marker_seen")

    return NegativeAudioEvaluation(
        inspected=bool(found_inspection_markers),
        accepted=bool(found_accept_markers),
        inspection_markers=found_inspection_markers,
        accept_markers=found_accept_markers,
        failure_reasons=tuple(failure_reasons),
        passed=not failure_reasons,
    )


def _parse_layers(raw_layers: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _LAYER_RE.finditer(raw_layers))
