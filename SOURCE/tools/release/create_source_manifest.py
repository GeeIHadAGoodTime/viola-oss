"""Create a source manifest for a checked-out public source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git_revision(source: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source.parent), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "working-tree"


def create(source: Path, revision: str) -> dict:
    rows = []
    try:
        tracked = subprocess.check_output(
            ["git", "-C", str(source.parent), "ls-files", "-z", "--", source.name],
            stderr=subprocess.DEVNULL,
        ).split(b"\0")
        paths = [source / item.decode("utf-8").removeprefix(source.name + "/") for item in tracked if item]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        paths = list(source.rglob("*"))
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source).as_posix()
        content = path.read_bytes()
        rows.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    rows.sort(key=lambda row: row["path"])
    tree_hash = hashlib.sha256("".join(f'{row["sha256"]}  {row["path"]}\n' for row in rows).encode()).hexdigest()
    return {"schema_version": 1, "revision": revision, "sha256": tree_hash, "files": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision")
    args = parser.parse_args()
    manifest = create(args.source.resolve(), args.revision or git_revision(args.source.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"revision": manifest["revision"], "files": len(manifest["files"]), "sha256": manifest["sha256"]}, indent=2))


if __name__ == "__main__":
    main()
