"""Verify a source snapshot against its external file manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify(source: Path, manifest: dict) -> list[str]:
    source = source.resolve()
    failures = []
    expected = set()
    aggregate = hashlib.sha256()
    for row in sorted(manifest["files"], key=lambda item: item["path"]):
        name = row["path"]
        path = PurePosixPath(name)
        if (
            not path.parts
            or any(ord(char) < 32 for char in name)
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
        ):
            failures.append("unsafe manifest path: " + name)
            continue
        if name in expected:
            failures.append("duplicate manifest path: " + name)
        expected.add(name)
        file = source / name
        if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(source):
            failures.append("missing or linked file: " + name)
            continue
        content = file.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        aggregate.update(f"{sha}  {name}\n".encode())
        if sha != row["sha256"] or len(content) != row["bytes"]:
            failures.append("content mismatch: " + name)
        if path.suffix == ".py":
            try:
                compile(content, name, "exec")
            except (SyntaxError, ValueError) as exc:
                failures.append(f"Python syntax error: {name}: {exc}")
    actual = {file.relative_to(source).as_posix() for file in source.rglob("*") if file.is_file()}
    failures.extend("extra file: " + name for name in sorted(actual - expected))
    if aggregate.hexdigest() != manifest["sha256"]:
        failures.append("aggregate content hash mismatch")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    failures = verify(args.source.resolve(), manifest)
    print(
        json.dumps({"revision": manifest["revision"], "files": len(manifest["files"]), "failures": failures}, indent=2)
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
