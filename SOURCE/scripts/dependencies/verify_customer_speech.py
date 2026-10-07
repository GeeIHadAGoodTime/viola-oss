"""Reject contaminated customer speech graphs and frozen payload inventories.

This is a dependency-separation check, not legal advice or release acceptance.
A passing source graph never stands in for frozen/native, signing, installed
speech or human listening evidence. The wider application's notices and every
resolved transitive dependency still need their ordinary license review.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath

REQUIRED = {
    "kokoro-onnx": "0.4.9+viola.3",
    "viola-misaki-en": "0.9.4+viola.1",
    "en-core-web-sm": "3.8.0",
    "spacy": "3.8.16",
    "numpy": "2.2.6",
    "regex": "2024.11.6",
    "addict": "2.4.0",
}
# Additional identities for explicitly requested, inactive CJK evidence only.
# Keep synchronized with the companion package and runtime admission map.
# These are required pins, not a complete transitive graph or license allowlist.
INACTIVE_CJK_REQUIRED = {
    "viola-misaki-cjk-prototype": "0.9.4+viola.cjk.4",
    "fugashi": "1.5.2",
    "jaconv": "0.5.0",
    "mojimoji": "0.0.13",
    "pypinyin": "0.55.0",
    "cn2an": "0.5.24",
    "jieba": "0.42.1",
    "ordered-set": "4.1.0",
    "proces": "0.1.7",
}
CJK_ROUTE_DEPENDENCIES = {
    "ja": ("fugashi", "jaconv", "mojimoji"),
    "zh": ("pypinyin", "cn2an", "jieba", "ordered-set", "proces"),
}
FORBIDDEN = frozenset(
    {"phonemizer", "phonemizer-fork", "espeakng-loader", "espeak-ng", "espeak", "num2words", "misaki"}
)
_FORBIDDEN_PAYLOAD = re.compile(
    r"(?:^|/)(?:phonemizer(?:[/.\-_]|$)|num2words(?:[/.\-_]|$)|(?:lib)?espeak[^/]*|internal-speech-notices(?:/|$)|internal-use-only\.json$)",
    re.IGNORECASE,
)


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.lower())


def validate_graph(
    report: dict, *, include_cjk_prototype: bool = False, cjk_locales: tuple[str, ...] | None = None
) -> list[str]:
    """Inspect a supplied pip report; never resolve, install or activate a profile.

    The explicit CJK option defaults to both retained routes for compatibility.
    A selected tuple checks only those routes' exact identities. It does not
    certify completeness, licensing, native loading or release use.
    """
    if type(include_cjk_prototype) is not bool:
        return ["inactive CJK evidence selection must be an explicit boolean"]
    if cjk_locales is not None and (
        not include_cjk_prototype
        or type(cjk_locales) is not tuple
        or not cjk_locales
        or any(type(locale) is not str or locale not in CJK_ROUTE_DEPENDENCIES for locale in cjk_locales)
        or len(set(cjk_locales)) != len(cjk_locales)
    ):
        return ["CJK locales require an explicit nonempty distinct ja/zh tuple in CJK mode"]
    rows = report.get("install")
    if not isinstance(rows, list) or not rows:
        return ["missing resolved distribution inventory"]
    errors = []
    observed = {}
    for row in rows:
        metadata = row.get("metadata", {}) if isinstance(row, dict) else {}
        raw_name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(raw_name, str) or not raw_name or not isinstance(version, str) or not version:
            errors.append("distribution is missing its exact name/version")
            continue
        name = canonical_name(raw_name)
        if name in observed:
            errors.append("duplicate distribution: " + name)
        observed[name] = version
        if name in FORBIDDEN:
            errors.append("forbidden customer speech dependency: " + name)
    required = dict(REQUIRED)
    if include_cjk_prototype:
        selected = {"viola-misaki-cjk-prototype"}
        for locale in cjk_locales if cjk_locales is not None else ("ja", "zh"):
            selected.update(CJK_ROUTE_DEPENDENCIES[locale])
        required.update({name: version for name, version in INACTIVE_CJK_REQUIRED.items() if name in selected})
    for name, version in required.items():
        if observed.get(name) != version:
            errors.append("missing or unreviewed customer speech distribution: " + name)
    if "onnxruntime" not in observed:
        errors.append("Kokoro ONNX runtime is missing")
    return errors


def validate_payload(paths: list[str]) -> list[str]:
    """Check complete frozen file and PYZ/module inventories supplied by builder.

    The builder must supply both inventories, since a filesystem-only check
    cannot establish that Python bytecode was excluded from a frozen archive.
    This function deliberately does not manufacture or certify that inventory.
    """
    if not paths:
        return ["missing frozen customer speech inventory"]
    errors = []
    for value in paths:
        if not isinstance(value, str) or not value:
            errors.append("invalid frozen inventory entry")
            continue
        value = value.replace("\\", "/")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or ":" in value:
            errors.append("non-relative frozen inventory entry")
            continue
        # Native/Python file paths and dotted Python module names are accepted.
        candidates = (value, value.replace(".", "/"))
        if any(_FORBIDDEN_PAYLOAD.search(candidate) for candidate in candidates):
            errors.append("forbidden customer speech payload: " + value)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pip-report", required=True, type=Path)
    parser.add_argument("--frozen-inventory", type=Path)
    parser.add_argument(
        "--include-cjk-prototype",
        action="store_true",
        help="Also require inactive CJK prototype pins; does not activate or qualify a customer profile",
    )
    parser.add_argument("--cjk-locales", choices=("ja", "zh"), nargs="+", help="Explicit CJK routes; default is both")
    args = parser.parse_args()
    try:
        errors = validate_graph(
            json.loads(args.pip_report.read_text(encoding="utf-8")),
            include_cjk_prototype=args.include_cjk_prototype,
            cjk_locales=tuple(args.cjk_locales) if args.cjk_locales is not None else None,
        )
        if args.frozen_inventory:
            data = json.loads(args.frozen_inventory.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"files", "python_modules"}:
                errors.append("frozen inventory must contain files and python_modules")
            elif not all(isinstance(data[key], list) and data[key] for key in data):
                errors.append("frozen file and Python module inventories must both be nonempty")
            else:
                errors.extend(validate_payload(data["files"] + data["python_modules"]))
    except (OSError, ValueError, TypeError) as exc:
        print("Invalid customer speech evidence: " + str(exc))
        return 1
    for error in errors:
        print(error)
    if not errors:
        if args.include_cjk_prototype:
            print(
                "Inactive CJK prototype dependency separation and pins passed; "
                "graph completeness, licensing and customer release remain unqualified"
            )
        else:
            print("Customer speech dependency separation passed; release and listening acceptance remain separate")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
