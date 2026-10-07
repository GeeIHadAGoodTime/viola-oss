"""Replay the immutable inactive Romance packet in a portable temporary fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent


def _read_bound_file(root: Path, row: dict) -> bytes:
    relative = PurePosixPath(row["path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"]:
        raise ValueError("Unsafe bound path")
    source = root.joinpath(*relative.parts)
    if source.is_symlink() or not source.is_file() or root.resolve() not in source.resolve().parents:
        raise ValueError(f"Missing or non-regular bound file: {relative}")
    data = source.read_bytes()
    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError(f"Bound file differs: {relative}")
    if "git_sha" in row:
        blob = b"blob " + str(len(data)).encode() + b"\0" + data
        # SHA256 above checks integrity; SHA1 only reproduces the upstream Git object ID.
        if hashlib.sha1(blob, usedforsecurity=False).hexdigest() != row["git_sha"]:
            raise ValueError(f"Upstream Git blob differs: {relative}")
    return data


def _validate_report_path(source_root: Path, report: Path, packet_root: Path = ROOT) -> Path:
    """Never turn a qualification output into a source-file overwrite."""
    report = report.resolve()
    roots = (source_root.resolve(), packet_root.resolve())
    if any(report == root or root in report.parents for root in roots):
        raise ValueError("Report destination overlaps immutable source inputs")
    if not report.parent.is_dir() or report.is_dir():
        raise ValueError("Report destination must have an existing external directory")
    if report.exists():
        identity = (report.stat().st_dev, report.stat().st_ino)
        for root in roots:
            for path in root.rglob("*"):
                if path.is_file():
                    stat = path.stat()
                    if (stat.st_dev, stat.st_ino) == identity:
                        raise ValueError("Report destination aliases immutable source inputs")
    return report


def _write_report(source_root: Path, report: Path, value: dict) -> None:
    report = _validate_report_path(source_root, report)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=report.parent, prefix=".romance-report-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, report)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_fixture(source_root: Path, destination: Path, packet_root: Path = ROOT) -> dict:
    """Verify before staging; only generated provenance paths are rebound."""
    integrity = json.loads((packet_root / "integrity.json").read_text(encoding="utf-8"))
    payloads = {row["path"]: _read_bound_file(packet_root, row) for row in integrity["payloads"]}
    config = _read_bound_file(source_root, integrity["kokoro_config"])
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in payloads.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    config_target = destination / "source" / integrity["kokoro_config"]["path"]
    config_target.parent.mkdir(parents=True, exist_ok=True)
    config_target.write_bytes(config)
    bindings_path = destination / "romance-prototype/upstream-bindings.json"
    original_bindings = bindings_path.read_bytes()
    bindings = json.loads(original_bindings)
    for row in bindings["files"]:
        row["file"] = str(destination / "upstream" / PurePosixPath(row["path"]).name)
    bindings_path.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
    return {
        "reviewed_patch_sha256": integrity["reviewed_patch_sha256"],
        "verified_payloads": len(payloads),
        "original_binding_sha256": hashlib.sha256(original_bindings).hexdigest(),
        "temporary_binding_sha256": hashlib.sha256(bindings_path.read_bytes()).hexdigest(),
        "temporary_binding_change": "only files[].file paths; immutable packet unchanged",
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "customer_profile_activated": False,
        "customer_or_release_eligible": False,
    }


_CHILD = """
import importlib.abc, runpy, sys
class DenyNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'onnxruntime', 'onnx', 'numpy', 'kokoro_onnx', 'misaki', 'spacy', 'phonemizer', 'espeakng_loader', 'piper_plus_g2p'}:
            raise AssertionError('Forbidden native/runtime import: ' + fullname)
sys.meta_path.insert(0, DenyNative())
root, mode = sys.argv[1:]
sys.path.insert(0, root)
if mode == 'tests':
    import pytest
    raise SystemExit(pytest.main(['-c', '/dev/null' if sys.platform != 'win32' else 'NUL', '-p', 'no:cacheprovider', root + '/test_romance_coverage.py', '-q']))
runpy.run_path(root + '/qualify_provenance.py', run_name='__main__')
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT.parents[1])
    parser.add_argument("--work-parent", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    report_path = _validate_report_path(source_root, args.report)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1")
    with tempfile.TemporaryDirectory(prefix="romance-source-qualification-", dir=args.work_parent) as temporary:
        fixture = Path(temporary) / "fixture"
        report = prepare_fixture(source_root, fixture)
        report["executions"] = []
        for mode in ("tests", "provenance"):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", _CHILD, str(fixture / "romance-prototype"), mode],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                check=False,
            )
            report["executions"].append(
                {"mode": mode, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            )
            if result.returncode:
                _write_report(source_root, report_path, report)
                return result.returncode
        report["provenance_result"] = json.loads(
            (fixture / "romance-prototype/provenance-and-mutants.json").read_text(encoding="utf-8")
        )
        # Confirm the published packet has not been changed by either replay.
        integrity = json.loads((ROOT / "integrity.json").read_text(encoding="utf-8"))
        for row in integrity["payloads"]:
            _read_bound_file(ROOT, row)
        report["immutable_payloads_preserved"] = True
        _write_report(source_root, report_path, report)
        print(
            json.dumps(
                {
                    "executions_passed": 2,
                    "verified_payloads": report["verified_payloads"],
                    "customer_or_release_eligible": False,
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
