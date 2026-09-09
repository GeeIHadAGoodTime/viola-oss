"""Build a versioned DeepFilterNet compatibility wheel from verified upstream bytes."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from urllib.request import urlopen

UPSTREAM_URL = "https://files.pythonhosted.org/packages/70/71/2edcc970c4dc689c301ea83e89a169fde08d6af0dfb26b14009ab27ee105/deepfilternet-0.5.6-py3-none-any.whl"
UPSTREAM_SHA256 = "99f5688d954fcfa8f853bf8bb8c3b2a59e4f9dc5d95643c9e6a32053234ba7c6"  # pragma: allowlist secret - public wheel checksum
PROJECT = tomllib.loads(Path(__file__).with_name("pyproject.toml").read_text(encoding="utf-8"))["project"]
VERSION = PROJECT["version"]
if PROJECT["name"].casefold() != "deepfilternet" or not re.fullmatch(r"0\.5\.6\+viola\.\d+", VERSION):
    raise ValueError("Project identity must describe a local patch of pinned DeepFilterNet 0.5.6")


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    local = os.environ.get("VIOLA_DEEPFILTER_UPSTREAM_WHEEL")
    if local:
        upstream = Path(local).read_bytes()
    else:
        with urlopen(UPSTREAM_URL, timeout=60) as response:  # nosec B310 - fixed HTTPS publisher URL
            upstream = response.read()
    if hashlib.sha256(upstream).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("DeepFilterNet upstream wheel checksum mismatch")
    source = Path(__file__).parent
    old_info, new_info = "deepfilternet-0.5.6.dist-info/", "deepfilternet-" + VERSION + ".dist-info/"
    with zipfile.ZipFile(io.BytesIO(upstream)) as archive:
        files = {
            name.replace(old_info, new_info): archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/RECORD")
        }
    files["df/io.py"] = (source / "io.py").read_bytes()
    checkpoint = files["df/checkpoint.py"].decode("utf-8")
    original_load = 'torch.load(latest, map_location="cpu")'
    if original_load not in checkpoint:
        raise ValueError("Pinned checkpoint loader changed; review patch before building")
    files["df/checkpoint.py"] = checkpoint.replace(
        original_load, 'torch.load(latest, map_location="cpu", weights_only=True)'
    ).encode("utf-8")
    metadata = files[new_info + "METADATA"].decode("utf-8").replace("Version: 0.5.6\n", "Version: " + VERSION + "\n", 1)
    if Parser().parsestr(metadata)["Name"].casefold() != PROJECT["name"].casefold():
        raise ValueError("Verified upstream name differs from project identity")
    metadata = metadata.replace(
        "\n\n",
        "\nRequires-Dist: torch (>=2.13)\nRequires-Dist: scipy (>=1,<2)\nRequires-Dist: soundfile (>=0.13.1)\n\n",
        1,
    )
    files[new_info + "METADATA"] = metadata.encode("utf-8")
    files[new_info + "LICENSE-MIT"] = (source / "LICENSE-MIT.txt").read_bytes()
    files[new_info + "LICENSE-APACHE-VIOLA"] = (source / "LICENSE-APACHE.txt").read_bytes()
    files[new_info + "VIOLA-PATCH-NOTICE.txt"] = (source / "README.md").read_bytes()
    rows = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        rows.append((name, "sha256=" + digest, str(len(content))))
    rows.append((new_info + "RECORD", "", ""))
    record = io.StringIO(newline="")
    csv.writer(record).writerows(rows)
    files[new_info + "RECORD"] = record.getvalue().encode("utf-8")
    filename = "deepfilternet-" + VERSION + "-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            wheel.writestr(info, content)
    return filename
