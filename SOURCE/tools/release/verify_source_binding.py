"""Verify the source tree, manifest, and retained public SBOM binding."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from verify_source import verify


EXPECTED_SBOMS = {
    "windows-desktop.sbom.json",
    "windows-kokoro.sbom.json",
    "windows-other-optional.sbom.json",
    "windows-all-optional.sbom.json",
    "windows-deepfilter.sbom.json",
    "frontend.sbom.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(source: Path, metadata: Path) -> list[str]:
    failures: list[str] = []
    manifest_path = metadata / "source-manifest.json"
    binding_path = metadata / "source-binding.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"metadata unreadable: {exc}"]

    if sha256(manifest_path) != binding.get("source_manifest_sha256"):
        failures.append("source manifest checksum mismatch")
    if manifest.get("sha256") != binding.get("source_tree_sha256"):
        failures.append("source tree checksum mismatch")
    if binding.get("source_file_count") != len(manifest.get("files", [])):
        failures.append("source file count mismatch")
    failures.extend(verify(source, manifest))

    retained = binding.get("retained_sboms", [])
    paths = [entry.get("path") for entry in retained]
    if set(paths) != EXPECTED_SBOMS or len(paths) != len(set(paths)):
        failures.append("retained SBOM scope set mismatch")
    for entry in retained:
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            failures.append("retained SBOM entry has no path")
            continue
        sbom_path = metadata / path_value
        if not sbom_path.is_file():
            failures.append(f"missing retained SBOM: {path_value}")
            continue
        if sha256(sbom_path) != entry.get("sha256"):
            failures.append(f"retained SBOM checksum mismatch: {path_value}")
        try:
            sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
            actual_components = len(sbom.get("components", []))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"invalid retained SBOM {path_value}: {exc}")
            continue
        if actual_components != entry.get("components"):
            failures.append(f"retained SBOM component count mismatch: {path_value}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--metadata", type=Path, default=Path.cwd().parent / "PUBLIC_METADATA")
    args = parser.parse_args()
    failures = check(args.source.resolve(), args.metadata.resolve())
    print(json.dumps({"files_checked": len(json.loads((args.metadata / "source-manifest.json").read_text())["files"]), "sboms_checked": len(EXPECTED_SBOMS), "failures": failures}, indent=2))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
