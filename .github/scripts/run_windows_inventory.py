"""Resolve each Windows feature scope in a fresh runtime, separate from SBOM tools."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

SCOPES = {
    "windows-desktop": [],
    "windows-kokoro": ["requirements_kokoro.txt"],
    "windows-other-optional": ["requirements_optional.txt"],
    "windows-all-optional": ["requirements_kokoro.txt", "requirements_optional.txt"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=SCOPES)
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 11):
        raise SystemExit("Run only on Windows CPython 3.11")
    root = Path(__file__).resolve().parents[2]
    source = root / "SOURCE"
    subprocess.run(
        [
            sys.executable,
            "-B",
            str(source / "tools/release/verify_source_binding.py"),
            "--source",
            str(source),
            "--metadata",
            str(root / "PUBLIC_METADATA"),
        ],
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="viola-inventory-") as temp:
        work = Path(temp)
        runtime = work / "runtime"
        tooling = work / "tooling"
        for env in (runtime, tooling):
            subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
        py = runtime / "Scripts/python.exe"
        tool_py = tooling / "Scripts/python.exe"
        bootstrap = work / "bootstrap-report.json"
        subprocess.run(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--force-reinstall",
                "--report",
                str(bootstrap),
                "pip==26.2.1",
                "setuptools==84.0.0",
            ],
            check=True,
        )
        report = work / "pip-report.json"
        command = [str(py), "-m", "pip", "install", "--report", str(report), "-r", "requirements_desktop.txt"]
        for requirement in SCOPES[args.scope]:
            command.extend(["-r", requirement])
        subprocess.run(command, cwd=source, check=True)
        subprocess.run([str(py), "-m", "pip", "check"], check=True)
        bootstrap_data = json.loads(bootstrap.read_text(encoding="utf-8"))
        runtime_data = json.loads(report.read_text(encoding="utf-8"))
        if bootstrap_data["environment"] != runtime_data["environment"]:
            raise ValueError("bootstrap and runtime marker environments differ")
        merged = {
            row["metadata"]["name"].lower().replace("_", "-"): row
            for row in bootstrap_data["install"] + runtime_data["install"]
        }
        runtime_data["install"] = list(merged.values())
        runtime_data["input_report_sha256"] = {
            "bootstrap": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
            "runtime": hashlib.sha256(report.read_bytes()).hexdigest(),
        }
        report.write_text(json.dumps(runtime_data), encoding="utf-8")
        installed = work / "installed.json"
        installed.write_bytes(subprocess.check_output([str(py), "-m", "pip", "list", "--format=json"]))
        subprocess.run([str(tool_py), "-m", "pip", "install", "cyclonedx-bom==7.3.0"], check=True)
        bom = work / "sbom.json"
        subprocess.run(
            [
                str(tool_py),
                "-m",
                "cyclonedx_py",
                "environment",
                "--pyproject",
                str(source / "pyproject.toml"),
                "--sv",
                "1.6",
                "--of",
                "JSON",
                "--output-file",
                str(bom),
                str(py),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(tool_py),
                str(root / ".github/scripts/inventory_evidence.py"),
                "--source",
                str(source),
                "--scope",
                args.scope,
                "--sbom",
                str(bom),
                "--report",
                str(report),
                "--installed",
                str(installed),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
