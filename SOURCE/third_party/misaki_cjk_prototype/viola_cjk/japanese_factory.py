"""New explicit Japanese factory bound to freshly recovered official dictionary.

No ambient dictionary, default Tagger constructor, download or model inference.
The source/API/schema has fresh static evidence; native behavior still needs its
own qualification. Historical missing factory receipts are not reused.
"""

from __future__ import annotations

from collections import namedtuple
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shlex

_MANIFEST_SHA256 = "5bdb8a6e9c5fbecd5b364cf284301f1dd44a715771ace8c6d19ccc6d31933cd7"
_MECABRC_SHA256 = "909a7044e39b38dbd39d0d44523ce3b9c14e41b11f7b55495621408633f3f779"


def _manifest():
    path = Path(__file__).with_name("unidic.json")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _MANIFEST_SHA256:
        raise ValueError("Japanese dictionary manifest differs")
    result = json.loads(raw)
    if len(result["files"]) != 18 or len({row["path"] for row in result["files"]}) != 18:
        raise ValueError("Japanese dictionary manifest is incomplete")
    fields = result["feature_fields"]
    if len(fields) != 29 or fields[9] != "pron" or fields[20] != "kana":
        raise ValueError("Japanese dictionary feature schema differs")
    return result


def validate_dictionary(dictionary_dir):
    # Capture the canonical identity before hashing; never return the movable
    # caller alias and resolve it again only after native construction.
    root = Path(dictionary_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("An explicit Japanese dictionary directory is required")
    manifest = _manifest()
    expected = {row["path"] for row in manifest["files"]}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Japanese dictionary files are incomplete or unexpected")
    for row in manifest["files"]:
        path = root / row["path"]
        if path.is_symlink() or path.resolve().parent != (root / Path(row["path"]).parent).resolve():
            raise ValueError("Japanese dictionary leaf must be an owned regular file")
        if path.stat().st_size != row["bytes"]:
            raise ValueError("Japanese dictionary file size differs: " + row["path"])
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != row["sha256"]:
            raise ValueError("Japanese dictionary file identity differs: " + row["path"])
    return root


def create_cutlet(dictionary_dir):
    root = validate_dictionary(dictionary_dir)
    manifest = _manifest()
    rc = Path(__file__).with_name("mecabrc").resolve(strict=True)
    if hashlib.sha256(rc.read_bytes()).hexdigest() != _MECABRC_SHA256:
        raise ValueError("Explicit Japanese runtime configuration differs")
    if importlib.metadata.version("fugashi") != "1.5.2":
        raise RuntimeError("The pinned Fugashi version is required")
    # Imports happen only after exact dictionary/notice/config validation.
    from fugashi import GenericTagger
    from misaki.cutlet import Cutlet, HEPBURN

    features = namedtuple("ViolaUniDicFeatures29", manifest["feature_fields"], defaults=(None,) * 29)
    arguments = "-r " + shlex.quote(str(rc)) + " -d " + shlex.quote(str(root))
    tagger = GenericTagger(arguments, wrapper=features)
    info = tagger.dictionary_info
    expected = manifest["system_dictionary"]
    if (
        not isinstance(info, list)
        or len(info) != 1
        or Path(info[0].get("filename", "")).resolve() != root / "sys.dic"
        or str(info[0].get("charset", "")).lower().replace("-", "") != expected["charset"]
        or info[0].get("version") != expected["version"]
        or info[0].get("size") != expected["lexsize"]
    ):
        raise ValueError("Native Japanese dictionary selection differs from the verified source")
    # Bypass Cutlet.__init__: its default Tagger probes ambient unidic packages.
    cutlet = Cutlet.__new__(Cutlet)
    cutlet.tagger = tagger
    cutlet.table = dict(HEPBURN)
    cutlet.exceptions = {}
    return cutlet
