"""Fail-closed Bandit JSON report-validation contract.

This stdlib suite runs under the existing public unittest discovery.
Workflow wiring assertions remain with the workflow consumer.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_bandit_report", ROOT / "scripts/ci/check_bandit_report.py")
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class BanditReportContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "bandit-report.json"

    def write_report(self, **overrides: object) -> Path:
        data = {
            "results": [],
            "errors": [],
            "generated_at": "2026-09-19T22:01:33Z",
            "metrics": {"_totals": {"loc": 5}, "app.py": {"loc": 5}},
        }
        data.update(overrides)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_missing_report_fails_closed(self) -> None:
        failures, highs = CHECK.check_report(self.path)
        assert "missing or unreadable" in failures[0]
        assert highs == []

    def test_unreadable_encoding_fails_closed(self) -> None:
        self.path.write_bytes(b"\xff")
        assert CHECK.check_report(self.path)[0]

    def test_malformed_or_incomplete_report_fails_closed(self) -> None:
        for text in ("{broken", "[]", '{"errors": []}', '{"results": []}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                assert CHECK.check_report(self.path)[0]

    def test_stub_or_uncredible_coverage_fails_closed(self) -> None:
        metadata_cases = [
            {},
            {"generated_at": "not-a-time", "metrics": {"_totals": {"loc": 5}, "app.py": {"loc": 5}}},
            {"generated_at": "2026-09-19T22:01:33Z", "metrics": {"_totals": {"loc": 0}}},
            {"generated_at": "2026-09-19T22:01:33Z", "metrics": {"_totals": {"loc": 5}}},
            {"generated_at": "2026-09-19T22:01:33Z", "metrics": {"_totals": {"loc": 5}, "app.py": {"loc": 0}}},
            {"generated_at": "2026-09-19T22:01:33Z", "metrics": {"_totals": {"loc": True}, "app.py": {"loc": 1}}},
        ]
        for metadata in metadata_cases:
            with self.subTest(metadata=metadata):
                self.path.write_text(json.dumps({"results": [], "errors": [], **metadata}), encoding="utf-8")
                failures, highs = CHECK.check_report(self.path)
                assert "metadata or source coverage" in failures[0]
                assert highs == []

    def test_scan_errors_fail_closed(self) -> None:
        assert CHECK.check_report(self.write_report(errors=["syntax error"]))[0]

    def test_unknown_severity_fails_closed(self) -> None:
        assert CHECK.check_report(self.write_report(results=[{"issue_severity": "UNKNOWN"}]))[0]

    def test_high_finding_fails(self) -> None:
        path = self.write_report(
            results=[{"issue_severity": "HIGH", "test_id": "B608", "filename": "app.py", "line_number": 7}]
        )
        failures, highs = CHECK.check_report(path)
        assert len(failures) == 1
        assert "HIGH" in failures[0]
        assert len(highs) == 1

    def test_medium_and_low_findings_preserve_existing_policy(self) -> None:
        for severity in ("LOW", "MEDIUM"):
            with self.subTest(severity=severity):
                assert CHECK.check_report(self.write_report(results=[{"issue_severity": severity}])) == ([], [])

    def test_valid_bandit_formatter_with_no_findings_passes(self) -> None:
        assert CHECK.check_report(self.write_report()) == ([], [])

    def test_cli_bounds_high_findings_without_source_snippets(self) -> None:
        finding = {
            "issue_severity": "HIGH",
            "test_id": "B608",
            "filename": "app.py",
            "line_number": 7,
            "code": "secret = value",
        }
        path = self.write_report(results=[finding] * 27)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert CHECK.main(["check_bandit_report.py", str(path)]) == 1
        output = stderr.getvalue()
        assert output.count("Bandit HIGH: B608 app.py:7") == 25
        assert "2 further HIGH finding(s) omitted" in output
        assert "secret = value" not in output

    def test_cli_missing_report_fails(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            assert CHECK.main(["check_bandit_report.py", str(self.path)]) == 1

    def test_cli_clean_report_passes(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            assert CHECK.main(["check_bandit_report.py", str(self.write_report())]) == 0


if __name__ == "__main__":
    unittest.main()
