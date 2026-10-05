"""Inactive Romance replay portability and bound-payload rejection controls."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
PACKET = SOURCE / "qualification/romance_semantic_lowering"
SPEC = importlib.util.spec_from_file_location("romance_packet_runner", PACKET / "run_qualification.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RomanceQualificationPacketTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.integrity = json.loads((PACKET / "integrity.json").read_text(encoding="utf-8"))

    def test_exact_packet_and_config_stage_with_only_temporary_path_rebinding(self):
        fixture = self.root / "fixture"
        report = RUNNER.prepare_fixture(SOURCE, fixture)
        self.assertEqual(report["verified_payloads"], 18)
        self.assertEqual(
            report["reviewed_patch_sha256"], "ac1491f19a3295931bda9cf72cb1649235bf75be742dccbb12a32e89fa63a030"
        )
        self.assertFalse(report["customer_profile_activated"])
        self.assertFalse(report["customer_or_release_eligible"])
        for row in self.integrity["payloads"]:
            with self.subTest(path=row["path"]):
                original = (PACKET / row["path"]).read_bytes()
                staged = (fixture / row["path"]).read_bytes()
                if row["path"] == "romance-prototype/upstream-bindings.json":
                    before, after = json.loads(original), json.loads(staged)
                    for old_row, new_row in zip(before["files"], after["files"], strict=True):
                        self.assertEqual(
                            Path(new_row["file"]).read_bytes(),
                            (PACKET / "upstream" / Path(old_row["path"]).name).read_bytes(),
                        )
                        new_row["file"] = old_row["file"]
                    self.assertEqual(after, before)
                else:
                    self.assertEqual(staged, original)

    def test_each_missing_payload_is_rejected_before_staging(self):
        packet = self.root / "packet"
        shutil.copytree(PACKET, packet)
        for row in self.integrity["payloads"]:
            path = packet / row["path"]
            data = path.read_bytes()
            path.unlink()
            try:
                with self.subTest(path=row["path"]), self.assertRaisesRegex(ValueError, "Missing"):
                    RUNNER.prepare_fixture(SOURCE, self.root / "must-not-exist", packet)
                self.assertFalse((self.root / "must-not-exist").exists())
            finally:
                path.write_bytes(data)

    def test_each_modified_payload_is_rejected_before_staging(self):
        packet = self.root / "packet"
        shutil.copytree(PACKET, packet)
        for row in self.integrity["payloads"]:
            path = packet / row["path"]
            data = path.read_bytes()
            path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            try:
                with self.subTest(path=row["path"]), self.assertRaisesRegex(ValueError, "differs"):
                    RUNNER.prepare_fixture(SOURCE, self.root / "must-not-exist", packet)
                self.assertFalse((self.root / "must-not-exist").exists())
            finally:
                path.write_bytes(data)

    def test_missing_and_different_model_vocabulary_fail_before_staging(self):
        source = self.root / "source"
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "Missing"):
            RUNNER.prepare_fixture(source, self.root / "must-not-exist")
        config = source / self.integrity["kokoro_config"]["path"]
        config.parent.mkdir(parents=True)
        config.write_text('{"vocab": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            RUNNER.prepare_fixture(source, self.root / "must-not-exist")
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_existing_destination_cannot_be_overwritten(self):
        fixture = self.root / "fixture"
        fixture.mkdir()
        witness = fixture / "preserved"
        witness.write_bytes(b"unchanged")
        with self.assertRaises(FileExistsError):
            RUNNER.prepare_fixture(SOURCE, fixture)
        self.assertEqual(witness.read_bytes(), b"unchanged")
        self.assertEqual(list(fixture.iterdir()), [witness])

    def _copied_source(self):
        # Even a broken guard must only be able to overwrite disposable inputs.
        source = self.root / "copied-source"
        packet = source / "qualification/romance_semantic_lowering"
        shutil.copytree(PACKET, packet)
        config = source / self.integrity["kokoro_config"]["path"]
        config.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / self.integrity["kokoro_config"]["path"], config)
        return source, packet

    def test_actual_cli_rejects_bound_output_collisions_before_running_children(self):
        source, packet = self._copied_source()
        for target in (
            packet / "romance-prototype/romance_coverage.py",
            packet / "integrity.json",
            packet / "run_qualification.py",
            source / self.integrity["kokoro_config"]["path"],
        ):
            with self.subTest(path=str(target)):
                before = target.read_bytes()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(packet / "run_qualification.py"),
                        "--source-root",
                        str(source),
                        "--report",
                        str(target),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Report destination overlaps immutable source inputs", result.stderr)
                self.assertEqual(target.read_bytes(), before)

    def test_external_hardlink_alias_is_rejected(self):
        source, packet = self._copied_source()
        target = packet / "romance-prototype/romance_coverage.py"
        alias = self.root / "alias.json"
        try:
            os.link(target, alias)
        except OSError:
            self.skipTest("Hardlink creation is not available on this filesystem")
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "aliases immutable"):
            RUNNER._validate_report_path(source, alias, packet)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(packet / "run_qualification.py"),
                "--source-root",
                str(source),
                "--report",
                str(alias),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Report destination aliases immutable source inputs", result.stderr)
        self.assertEqual(target.read_bytes(), before)

    def test_safe_external_report_is_written_atomically(self):
        report = self.root / "report.json"
        RUNNER._write_report(SOURCE, report, {"value": "témoignage", "customer_or_release_eligible": False})
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["value"], "témoignage")
        self.assertEqual(list(self.root.iterdir()), [report])
        self.assertEqual(RUNNER._validate_report_path(SOURCE, report), report.resolve())

    def test_atomic_external_report_replacement_preserves_an_unrelated_link(self):
        original = self.root / "original.json"
        original.write_bytes(b"preserved")
        report = self.root / "report.json"
        try:
            os.link(original, report)
        except OSError:
            self.skipTest("Hardlink creation is not available on this filesystem")
        RUNNER._write_report(SOURCE, report, {"value": "new report"})
        self.assertEqual(original.read_bytes(), b"preserved")
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"value": "new report"})

    def test_unsafe_manifest_paths_and_git_identity_drift_reject(self):
        row = self.integrity["payloads"][-1]
        for path in ("../outside", "/absolute", "upstream\\base.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Unsafe"):
                RUNNER._read_bound_file(PACKET, dict(row, path=path))
        with self.assertRaisesRegex(ValueError, "Git blob differs"):
            RUNNER._read_bound_file(PACKET, dict(row, git_sha="0" * 40))


if __name__ == "__main__":
    unittest.main()
