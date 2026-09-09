"""Verify vendored sources and exact public dependency resolution reports.

Ordinary verification is offline. --upstream reconstructs each fork from the
pinned public archive and reviewed patch in a temporary directory (requires Git).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]


def canonical_bytes(data: bytes) -> bytes:
    """Normalize text line endings; keep binary model/audio bytes unchanged."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if "\0" in text:
        return data
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_graph(report: dict, *, kokoro: bool = False) -> list[str]:
    """Reject reintroduced dependencies using the complete resolver output."""
    forbidden = {"nltk", "espeakng-loader", "misaki"}
    if not kokoro:
        forbidden |= {"kokoro-onnx", "phonemizer-fork"}
    errors = []
    items = report.get("install")
    if not items:
        return ["empty or missing resolved distribution list"]
    names = set()
    for item in items:
        meta = item.get("metadata", {})
        name = re.sub(r"[-_.]+", "-", meta.get("name", "").lower())
        names.add(name)
        if name in forbidden:
            errors.append("forbidden public dependency: " + name)
        if not meta.get("version"):
            errors.append("missing exact version: " + name)
    if "pipecat-ai" not in names or "pysbd" not in names:
        errors.append("retained voice runtime or tokenizer missing")
    return errors


def verify_sources(root: Path, *, upstream: bool = False, tracked: bool = False) -> list[str]:
    errors = []
    tracked_paths = set()
    if tracked:
        tracked_paths = set(
            subprocess.check_output(["git", "ls-files", "-z", "--", "third_party"], cwd=root)
            .decode("utf-8")
            .split("\0")
        )
    records = tomllib.loads((root / "third_party/sources.toml").read_text(encoding="utf-8"))
    for key, record in records.items():
        package_root = root / "third_party" / key
        for path in package_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(package_root)
            if any(
                part in {"build", "dist", "__pycache__", ".ruff_cache"} or part.endswith(".egg-info")
                for part in relative.parts
            ):
                continue
            if relative.as_posix() not in record["files"]:
                errors.append("unrecorded source: " + key + "/" + relative.as_posix())
        for relative, expected in record["files"].items():
            if tracked and f"third_party/{key}/{relative}" not in tracked_paths:
                errors.append("untracked required dependency source: " + key + "/" + relative)
            path = root / "third_party" / key / relative
            if not path.is_file() or digest(canonical_bytes(path.read_bytes())) != expected:
                errors.append("changed or missing source: " + key + "/" + relative)
        if upstream:
            archive = urllib.request.urlopen(record["source_url"], timeout=60).read()
            if digest(archive) != record["source_sha256"]:
                errors.append("upstream archive checksum mismatch: " + key)
                continue
            with tempfile.TemporaryDirectory(prefix="viola-source-review-") as temp:
                workspace = Path(temp)
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                    for member in bundle.getmembers():
                        relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
                        if str(relative) not in record["upstream_files"]:
                            continue
                        if not member.isfile() or ".." in relative.parts:
                            raise ValueError("unsafe archive entry")
                        data = bundle.extractfile(member).read()
                        if digest(data) != record["upstream_files"][str(relative)]["sha256"]:
                            raise ValueError("upstream file checksum mismatch")
                        target = workspace / key / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(canonical_bytes(data))
                patch = workspace / "reviewed.patch"
                # Review comments annotate removed upstream placeholder lines for
                # credential scanning. They are metadata, not patch operations.
                patch_text = (root / "third_party" / record["patch"]).read_text(encoding="utf-8")
                patch.write_text(
                    "\n".join(
                        line
                        for line in patch_text.splitlines()
                        if line != "# pragma: allowlist nextline secret (known upstream placeholder)"
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                subprocess.run(
                    ["git", "-c", "core.autocrlf=false", "apply", "--unidiff-zero", "--whitespace=nowarn", str(patch)],
                    cwd=workspace,
                    check=True,
                )
                for relative in record["upstream_files"]:
                    rebuilt = workspace / key / relative
                    if digest(rebuilt.read_bytes()) != record["files"][relative]:
                        errors.append("reconstructed source differs: " + key + "/" + relative)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", action="store_true")
    parser.add_argument("--tracked", action="store_true", help="Require every source/model in the Git index")
    parser.add_argument("--pip-report", type=Path)
    parser.add_argument("--kokoro", action="store_true")
    args = parser.parse_args()
    errors = verify_sources(ROOT, upstream=args.upstream, tracked=args.tracked)
    if args.pip_report:
        errors += validate_graph(json.loads(args.pip_report.read_text(encoding="utf-8")), kokoro=args.kokoro)
    for error in errors:
        print(error)
    if not errors:
        print("Verified maintained dependency sources" + (" and resolved graph" if args.pip_report else ""))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
