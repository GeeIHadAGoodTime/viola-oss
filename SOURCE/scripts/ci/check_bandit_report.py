#!/usr/bin/env python3
"""Fail closed on missing or incomplete Bandit JSON, then enforce HIGH findings.

Bandit runs with ``--exit-zero`` in CI so findings do not stop report creation.
This check therefore owns the security verdict. It prints a bounded identifier
list for HIGH findings so quota failures do not hide which rules fired, but the
job log is not a replacement for the downloadable JSON report.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

MAX_FINDINGS_IN_LOG = 25
SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH"})


def has_scan_coverage(data: dict) -> bool:
    """Require Bandit's JSON metadata and nonempty, consistent source coverage."""
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        return False
    try:
        parsed_at = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    if parsed_at.strftime("%Y-%m-%dT%H:%M:%SZ") != generated_at:
        return False

    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        return False
    totals = metrics.get("_totals")
    if not isinstance(totals, dict):
        return False
    total_loc = totals.get("loc")
    if type(total_loc) is not int or total_loc <= 0:
        return False

    file_locs = []
    for name, metric in metrics.items():
        if name == "_totals":
            continue
        if not isinstance(name, str) or not name or not isinstance(metric, dict):
            return False
        loc = metric.get("loc")
        if type(loc) is not int or loc < 0:
            return False
        file_locs.append(loc)
    return bool(file_locs) and sum(file_locs) == total_loc


def check_report(path: Path) -> tuple[list[str], list[dict]]:
    """Return report failures and HIGH findings without trusting absent keys."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"Bandit report is missing or unreadable: {exc}"], []

    if not isinstance(data, dict):
        return ["Bandit report root must be a JSON object"], []
    results = data.get("results")
    errors = data.get("errors")
    if not isinstance(results, list) or not isinstance(errors, list):
        return ["Bandit report must contain results and errors arrays"], []
    if errors:
        return [f"Bandit reported {len(errors)} scan error(s)"], []
    if not has_scan_coverage(data):
        return ["Bandit report lacks valid formatter metadata or source coverage"], []

    highs: list[dict] = []
    for index, finding in enumerate(results):
        if not isinstance(finding, dict) or finding.get("issue_severity") not in SEVERITIES:
            return [f"Bandit result {index} has no recognized issue_severity"], []
        if finding["issue_severity"] == "HIGH":
            highs.append(finding)
    if highs:
        return [f"Found {len(highs)} HIGH severity security issue(s) in Bandit scan"], highs
    return [], []


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_bandit_report.py <bandit-report.json>", file=sys.stderr)
        return 2
    failures, highs = check_report(Path(argv[1]))
    for finding in highs[:MAX_FINDINGS_IN_LOG]:
        test_id = str(finding.get("test_id", "?"))
        filename = str(finding.get("filename", "?"))
        line = str(finding.get("line_number", "?"))
        print(f"Bandit HIGH: {test_id} {filename}:{line}", file=sys.stderr)
    if len(highs) > MAX_FINDINGS_IN_LOG:
        print(f"... {len(highs) - MAX_FINDINGS_IN_LOG} further HIGH finding(s) omitted", file=sys.stderr)
    for failure in failures:
        print(f"::error::{failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
