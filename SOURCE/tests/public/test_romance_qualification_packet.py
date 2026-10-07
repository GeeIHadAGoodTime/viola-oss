"""Inactive Romance replay portability and bound-payload rejection controls."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
PACKET = SOURCE / "qualification/romance_semantic_lowering"
SPEC = importlib.util.spec_from_file_location("romance_packet_runner", PACKET / "run_qualification.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RomanceQualificationPacketTests(unittest.TestCase):
    def setUp(self):
        # Application startup can pin the process-wide temp root inside SOURCE.
        # Positive report fixtures must remain outside the protected inputs.
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="romance-test-", dir=SOURCE.parent))
        self.root = Path(self.temporary)
        self.integrity = json.loads((PACKET / "integrity.json").read_text(encoding="utf-8"))

    def test_fixture_stays_external_when_application_pins_the_default_temp_root(self):
        with patch.object(tempfile, "tempdir", str(SOURCE)):
            probe = RomanceQualificationPacketTests("test_existing_destination_cannot_be_overwritten")
            try:
                probe.setUp()
                self.assertNotEqual(probe.root, SOURCE)
                self.assertNotIn(SOURCE, probe.root.parents)
                self.assertNotIn(PACKET, probe.root.parents)
                self.assertEqual(probe.root.parent, SOURCE.parent)
            finally:
                probe.doCleanups()

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

    def test_git_identities_work_when_sha1_is_allowed_only_for_nonsecurity_use(self):
        original_sha1 = hashlib.sha1
        blobs = []

        def identity_only(data=b"", *, usedforsecurity=True):
            self.assertIs(usedforsecurity, False)
            blobs.append(data)
            return original_sha1(data, usedforsecurity=False)

        upstream = [row for row in self.integrity["payloads"] if "git_sha" in row]
        with patch.object(RUNNER.hashlib, "sha1", side_effect=identity_only):
            for row in upstream:
                with self.subTest(path=row["path"]):
                    data = RUNNER._read_bound_file(PACKET, row)
                    self.assertEqual(blobs[-1], b"blob " + str(len(data)).encode() + b"\0" + data)
        self.assertEqual(len(blobs), 5)

    def test_sha256_failure_rejects_before_a_matching_git_identity_can_be_used(self):
        upstream = [row for row in self.integrity["payloads"] if "git_sha" in row]
        with patch.object(RUNNER.hashlib, "sha1", side_effect=AssertionError("Git identity used before integrity")):
            for row in upstream:
                with self.subTest(path=row["path"]), self.assertRaisesRegex(ValueError, "Bound file differs"):
                    RUNNER._read_bound_file(PACKET, dict(row, sha256="0" * 64))

    def _run_guarded_provenance(self, fixture):
        # Execute the qualifier's unmodified provenance block with stdlib only.
        # The separate full packet replay also covers its pytest-based mutants.
        script = """
import ast, hashlib, json, sys
from pathlib import Path
path = Path(sys.argv[1])
tree = ast.parse(path.read_text(encoding='utf-8'))
def assignment(node, name):
    return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
start = next(i for i, node in enumerate(tree.body) if assignment(node, 'records'))
end = next(i for i, node in enumerate(tree.body) if assignment(node, 'source'))
scope = dict(ast=ast, hashlib=hashlib, json=json, Path=Path, ROOT=path.parent)
original_sha1 = hashlib.sha1
calls = []
def identity_only(data=b'', *, usedforsecurity=True):
    if usedforsecurity is not False:
        raise RuntimeError('SHA1 is allowed only for nonsecurity Git identity')
    calls.append(data)
    return original_sha1(data, usedforsecurity=False)
hashlib.sha1 = identity_only
try:
    exec(compile(ast.Module(body=tree.body[start:end], type_ignores=[]), str(path), 'exec'), scope)
    print(json.dumps(scope['checked']))
finally:
    print('git_identity_calls=' + str(len(calls)))
"""
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(fixture / "romance-prototype/qualify_provenance.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def test_provenance_block_preserves_git_identities_under_nonsecurity_only_sha1(self):
        fixture = self.root / "fixture"
        RUNNER.prepare_fixture(SOURCE, fixture)
        result = self._run_guarded_provenance(fixture)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("git_identity_calls=5", result.stdout)
        checked = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(len(checked), 5)
        self.assertEqual(sum(row.get("exact_original", False) for row in checked), 2)
        self.assertEqual(sum(row.get("residual_ast_exact", False) for row in checked), 3)

    def test_provenance_sha256_rejects_before_matching_git_identity(self):
        fixture = self.root / "fixture"
        RUNNER.prepare_fixture(SOURCE, fixture)
        bindings_path = fixture / "romance-prototype/upstream-bindings.json"
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
        bindings["files"][0]["sha256"] = "0" * 64
        bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
        result = self._run_guarded_provenance(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("git_identity_calls=0", result.stdout)
        self.assertFalse((fixture / "romance-prototype/provenance-and-mutants.json").exists())

    def test_provenance_git_identity_drift_still_rejects_after_sha256(self):
        fixture = self.root / "fixture"
        RUNNER.prepare_fixture(SOURCE, fixture)
        bindings_path = fixture / "romance-prototype/upstream-bindings.json"
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
        bindings["files"][0]["git_sha"] = "0" * 40
        bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
        result = self._run_guarded_provenance(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("git_identity_calls=1", result.stdout)
        self.assertFalse((fixture / "romance-prototype/provenance-and-mutants.json").exists())


if __name__ == "__main__":
    unittest.main()
