"""Replay the immutable inactive Italian/Hindi packet in a portable temporary fixture."""

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
        if hashlib.sha1(blob).hexdigest() != row["git_sha"]:
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
            mode="w", encoding="utf-8", dir=report.parent, prefix=".subset-report-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, report)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_fixture(source_root: Path, destination: Path, packet_root: Path = ROOT) -> dict:
    """Stage the twenty-one unchanged inputs only after source and model binding."""
    integrity = json.loads((packet_root / "integrity.json").read_text(encoding="utf-8"))
    payloads = {row["path"]: _read_bound_file(packet_root, row) for row in integrity["payloads"]}
    if len(payloads) != 21 or len(payloads) != len(integrity["payloads"]):
        raise ValueError("The reviewed twenty-one-file packet is incomplete or duplicated")
    config = _read_bound_file(source_root, integrity["kokoro_config"])
    if config != payloads["packet/evidence/kokoro-config.json"]:
        raise ValueError("The packet vocabulary differs from the current Kokoro configuration")
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in payloads.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return integrity


_CHILD = """
import builtins, runpy, socket, sys
original_import = builtins.__import__
forbidden = {'epitran', 'panphon', 'numpy', 'torch', 'onnxruntime', 'onnx', 'regex', 'misaki', 'kokoro_onnx', 'spacy', 'phonemizer'}
def checked_import(name, *args, **kwargs):
    if name.split('.', 1)[0] in forbidden:
        raise AssertionError('Forbidden native/runtime import: ' + name)
    return original_import(name, *args, **kwargs)
def no_network(*args, **kwargs):
    raise AssertionError('Qualification attempted network access')
builtins.__import__ = checked_import
for name in ('socket', 'create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex'):
    setattr(socket, name, no_network)
script, *arguments = sys.argv[1:]
sys.argv = [script, *arguments]
runpy.run_path(script, run_name='__main__')
"""


def _check_results(fixture: Path, integrity: dict) -> dict:
    controls = json.loads((fixture / "controls.json").read_text(encoding="utf-8"))
    expected = integrity["expected"]
    if (
        controls.get("tests") != expected["methods"]
        or controls.get("subtests") != expected["subtests"]
        or any(controls.get(key) != 0 for key in ("failures", "errors", "skipped"))
        or controls.get("socket_attempts") != []
        or controls.get("forbidden_modules_loaded") != []
        or controls.get("source_unchanged") is not True
        or controls.get("source_before") != controls.get("source_after")
    ):
        raise ValueError("The guarded source controls are incomplete or failed")
    mutations = json.loads((fixture / "packet/evidence/mutation-summary.json").read_text(encoding="utf-8"))
    rows = mutations.get("mutants", [])
    if (
        len(rows) != expected["mutants"]
        or len({row.get("name") for row in rows}) != expected["mutants"]
        or mutations.get("author_source_unchanged") is not True
        or mutations.get("author_source_before") != mutations.get("author_source_after")
        or any(
            row.get("assertion_sensitive") is not True
            or row.get("exit") != 1
            or not isinstance(row.get("failures"), int)
            or row["failures"] < 1
            or row.get("errors") != 0
            or row.get("tests") != expected["methods"]
            or row.get("socket_attempts") != []
            or row.get("source_unchanged") is not True
            for row in rows
        )
    ):
        raise ValueError("The mutation controls are incomplete or failed")
    return {"controls": controls, "mutations": mutations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT.parents[1])
    parser.add_argument("--work-parent", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    report_path = _validate_report_path(source_root, args.report)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="italian-hindi-qualification-", dir=args.work_parent) as temporary:
        fixture = Path(temporary) / "fixture"
        integrity = prepare_fixture(source_root, fixture)
        report = {
            "reviewed_core_patch_sha256": integrity["reviewed_core_patch_sha256"],
            "verified_payloads": len(integrity["payloads"]),
            "kokoro_config_sha256": integrity["kokoro_config"]["sha256"],
            "executions": [],
            "customer_profile_activated": False,
            "customer_or_release_eligible": False,
            "native_speech_executed": False,
        }
        for script, arguments in (
            ("run_controls.py", ["--output", str(fixture / "controls.json")]),
            ("run_mutations.py", []),
        ):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", _CHILD, str(fixture / "packet" / script), *arguments],
                env=environment,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            report["executions"].append(
                {"script": script, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            )
            if result.returncode:
                _write_report(source_root, report_path, report)
                return result.returncode
        report.update(_check_results(fixture, integrity))
        for row in integrity["payloads"]:
            _read_bound_file(ROOT, row)
        report["immutable_payloads_preserved"] = True
        _write_report(source_root, report_path, report)
        print(json.dumps({"methods": 19, "subtests": 412, "mutants": 16, "customer_or_release_eligible": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
