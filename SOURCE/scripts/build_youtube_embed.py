"""Export only the standalone YouTube helper, without the company website."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "embed.html": "ViolaWebsite/embed.html",
    "js/embed.js": "ViolaWebsite/js/embed.js",
}
HEADERS = """/*
  Content-Security-Policy: default-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://www.youtube.com https://*.youtube.com https://*.ytimg.com; frame-src https://www.youtube.com https://www.youtube-nocookie.com; frame-ancestors http: https:; object-src 'none'; base-uri 'none'; form-action 'none'
  Referrer-Policy: strict-origin-when-cross-origin
  X-Content-Type-Options: nosniff
  Permissions-Policy: camera=(), microphone=(), geolocation=()
"""


def build(output: Path, *, source_root: Path = ROOT) -> list[Path]:
    """Create a fresh static directory; never overwrite an existing deployment."""
    contents = {name: (source_root / source).read_bytes() for name, source in SOURCES.items()}
    output.mkdir(parents=True, exist_ok=False)
    written = []
    for name, content in {**contents, "_headers": HEADERS.encode("utf-8")}.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New directory for the three standalone static files",
    )
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
