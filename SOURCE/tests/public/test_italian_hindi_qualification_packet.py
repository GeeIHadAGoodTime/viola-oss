"""Portable publication contracts for the inactive, immutable word experiments."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
PACKET = SOURCE / "qualification/italian_hindi_subsets"


def load_wrapper(path: Path):
    spec = importlib.util.spec_from_file_location("_italian_hindi_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ItalianHindiPacketTests(unittest.TestCase):
    def setUp(self):
        # Public tests may set the global temporary directory inside SOURCE.
        # Positive reports and all destructive negative fixtures stay outside it.
        self.temporary = tempfile.TemporaryDirectory(prefix="Italian Hindi '日本' ", dir=SOURCE.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "copy with spaces" / "SOURCE"
        self.packet = self.source / "qualification/italian_hindi_subsets"
        shutil.copytree(PACKET, self.packet)
        self.integrity = json.loads((self.packet / "integrity.json").read_text(encoding="utf-8"))
        config = self.integrity["kokoro_config"]["path"]
        target = self.source / config
        target.parent.mkdir(parents=True)
        shutil.copyfile(SOURCE / config, target)
        self.wrapper = load_wrapper(self.packet / "run_qualification.py")

    def snapshot(self):
        return {
            str(path.relative_to(self.source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.source.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }

    def cli(self, report, *extra):
        return subprocess.run(
            [sys.executable, "-B", str(self.packet / "run_qualification.py"), "--report", str(report), *extra],
            cwd=self.root,
            env=dict(os.environ, PYTHONUTF8="0", PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            encoding="utf-8",
            timeout=45,
        )

    def test_actual_portable_replay_preserves_all_inputs_and_cleans_work(self):
        before = self.snapshot()
        work = self.root / "temporary work"
        work.mkdir()
        report = self.root / "report.json"
        result = self.cli(report, "--work-parent", str(work))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((data["controls"]["tests"], data["controls"]["subtests"]), (19, 412))
        self.assertEqual(data["controls"]["socket_attempts"], [])
        self.assertEqual(data["controls"]["forbidden_modules_loaded"], [])
        self.assertEqual(len(data["mutations"]["mutants"]), 16)
        self.assertTrue(all(row["assertion_sensitive"] for row in data["mutations"]["mutants"]))
        self.assertTrue(data["immutable_payloads_preserved"])
        self.assertFalse(data["customer_profile_activated"])
        self.assertFalse(data["customer_or_release_eligible"])
        self.assertFalse(data["native_speech_executed"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(work.iterdir()), [])

    def test_all_missing_or_altered_payloads_reject_before_fixture_creation(self):
        for row in self.integrity["payloads"]:
            path = self.packet / row["path"]
            original = path.read_bytes()
            for mode in ("missing", "altered"):
                with self.subTest(path=row["path"], mode=mode):
                    destination = self.root / "not-created"
                    try:
                        if mode == "missing":
                            path.unlink()
                        else:
                            path.write_bytes(original + b"unreviewed")
                        with self.assertRaises(ValueError):
                            self.wrapper.prepare_fixture(self.source, destination)
                        self.assertFalse(destination.exists())
                    finally:
                        path.write_bytes(original)
                        if destination.exists():
                            shutil.rmtree(destination)

    def test_changed_live_model_vocabulary_rejects_before_fixture_creation(self):
        config = self.source / self.integrity["kokoro_config"]["path"]
        config.write_bytes(config.read_bytes() + b" ")
        destination = self.root / "not-created"
        with self.assertRaisesRegex(ValueError, "Bound file differs"):
            self.wrapper.prepare_fixture(self.source, destination)
        self.assertFalse(destination.exists())

    def test_unsafe_or_duplicate_manifest_paths_reject(self):
        for invalid in ("../outside", "/outside", "packet\\outside"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                self.wrapper._read_bound_file(self.packet, dict(self.integrity["payloads"][0], path=invalid))
        changed = copy.deepcopy(self.integrity)
        changed["payloads"].append(changed["payloads"][0])
        (self.packet / "integrity.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete or duplicated"):
            self.wrapper.prepare_fixture(self.source, self.root / "not-created")

    def test_actual_cli_rejects_source_and_hardlink_report_collisions(self):
        paths = [
            self.packet / "packet/italian-stress-prototype/italian_stress.py",
            self.packet / "packet/hindi-word-prototype/hindi_word.py",
            self.packet / "integrity.json",
            self.packet / "run_qualification.py",
            self.source / self.integrity["kokoro_config"]["path"],
        ]
        alias = self.root / "external-hardlink.json"
        os.link(paths[0], alias)
        for report in [*paths, alias]:
            with self.subTest(report=str(report)):
                before = self.snapshot()
                result = self.cli(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Report destination", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_safe_external_report_ignores_source_local_temp_defaults(self):
        inner_temp = self.source / ".viola/tmp"
        inner_temp.mkdir(parents=True)
        report = self.root / "external.json"
        with patch.object(tempfile, "tempdir", str(inner_temp)):
            self.wrapper._write_report(self.source, report, {"synthetic": True})
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"synthetic": True})
        self.assertEqual(list(inner_temp.iterdir()), [])

    def test_safe_report_replaces_an_unrelated_hardlink_atomically(self):
        original = self.root / "unrelated.txt"
        original.write_bytes(b"preserve unrelated file")
        report = self.root / "external.json"
        os.link(original, report)
        self.wrapper._write_report(self.source, report, {"fresh": True})
        self.assertEqual(original.read_bytes(), b"preserve unrelated file")
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"fresh": True})
        self.assertEqual(list(self.root.glob(".subset-report-*")), [])

    def test_child_failure_propagates_without_success_claim(self):
        report = self.root / "failed.json"
        result = subprocess.CompletedProcess([], 7, "controlled failure", "synthetic child failure")
        with (
            patch.object(sys, "argv", ["runner", "--source-root", str(self.source), "--report", str(report)]),
            patch.object(self.wrapper.subprocess, "run", return_value=result) as run,
        ):
            self.assertEqual(self.wrapper.main(), 7)
        self.assertEqual(run.call_count, 1)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertNotIn("immutable_payloads_preserved", data)
        self.assertEqual(data["executions"][0]["returncode"], 7)
        self.assertFalse(data["customer_or_release_eligible"])
        args = run.call_args
        self.assertEqual(args.kwargs["encoding"], "utf-8")
        self.assertEqual(args.kwargs["env"]["PYTHONUTF8"], "1")
        self.assertIn("utf8", args.args[0])


if __name__ == "__main__":
    unittest.main()
