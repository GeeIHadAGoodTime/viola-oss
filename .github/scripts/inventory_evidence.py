"""Bind installed dependency inventories to public source and emit bounded receipts."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import platform
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import url2pathname

try:
    from packaging.requirements import Requirement
    from packaging.version import Version
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def verify_expected_source(source: Path, manifest: dict) -> None:
    rows = manifest["files"]
    paths = [row["path"] for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate source manifest path")
    computed = []
    for row in rows:
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or ":" in row["path"]:
            raise ValueError("unsafe source manifest path")
        path = source / relative
        if not path.resolve(strict=True).is_relative_to(source.resolve(strict=True)):
            raise ValueError("source manifest path escapes root")
        data = path.read_bytes()
        if path.is_symlink() or len(data) != row["bytes"] or digest(data) != row["sha256"]:
            raise ValueError("source bytes differ from manifest: " + row["path"])
        computed.append(f'{row["sha256"]}  {row["path"]}\n')
    if digest("".join(computed).encode()) != manifest["sha256"]:
        raise ValueError("manifest source tree digest mismatch")


def validate_graph(bom: dict) -> None:
    refs = [c["bom-ref"] for c in bom["components"]]
    if len(refs) != len(set(refs)):
        raise ValueError("duplicate component reference")
    root = bom.get("metadata", {}).get("component", {}).get("bom-ref")
    allowed = set(refs) | ({root} if root else set())
    rows = bom["dependencies"]
    nodes = [row["ref"] for row in rows]
    if len(nodes) != len(set(nodes)) or not set(refs).issubset(nodes) or not set(nodes).issubset(allowed):
        raise ValueError("incomplete or duplicate dependency graph")
    if any(not set(row.get("dependsOn", [])).issubset(allowed) for row in rows):
        raise ValueError("dangling dependency graph reference")


def active_dependencies(report: dict, components: dict, root_ref: str) -> list[dict]:
    rows = {canonical(x["metadata"]["name"]): x for x in report["install"]}
    extras = {name: set(row.get("requested_extras", [])) for name, row in rows.items()}
    edges = {name: set() for name in rows}
    changed = True
    while changed:
        changed = False
        for name, row in rows.items():
            for text in row["metadata"].get("requires_dist", []):
                requirement = Requirement(text)
                if requirement.marker and not any(
                    requirement.marker.evaluate({**report["environment"], "extra": extra})
                    for extra in {""} | extras[name]
                ):
                    continue
                dependency = canonical(requirement.name)
                if dependency not in rows or not requirement.specifier.contains(
                    rows[dependency]["metadata"]["version"], prereleases=True
                ):
                    raise ValueError("active dependency missing or incompatible: " + text)
                edges[name].add(dependency)
                added = set(requirement.extras) - extras[dependency]
                if added:
                    extras[dependency].update(added)
                    changed = True
    output = [
        {
            "ref": root_ref,
            "dependsOn": sorted(components[n]["bom-ref"] for n, row in rows.items() if row.get("requested")),
        }
    ]
    output.extend(
        {"ref": components[n]["bom-ref"], "dependsOn": sorted(components[d]["bom-ref"] for d in edges[n])}
        for n in sorted(rows)
    )
    return output


def bind_python(source: Path, scope: str, report: dict, installed: list, bom: dict) -> dict:
    if report.get("version") != "1":
        raise ValueError("unsupported pip report version")
    env = report["environment"]
    if env.get("sys_platform") != "win32" or env.get("python_version") != "3.11":
        raise ValueError("Windows CPython 3.11 evidence required")
    if env.get("implementation_name") != "cpython":
        raise ValueError("CPython evidence required")
    expected = {canonical(x["metadata"]["name"]): x["metadata"]["version"] for x in report["install"]}
    actual = {canonical(x["name"]): x["version"] for x in installed}
    if not expected or actual != expected:
        raise ValueError("pip report does not match installed versions")
    components = {canonical(x["name"]): x for x in bom["components"]}
    if (
        len(components) != len(bom["components"])
        or set(components) != set(expected)
        or any(components.get(n, {}).get("version") != v for n, v in expected.items())
    ):
        raise ValueError("SBOM does not cover exact installed dependency graph")
    validate_graph(bom)
    if scope == "windows-deepfilter":
        if (
            "pyjwt" in expected
            or expected.get("numpy") != "1.26.4"
            or Version(expected.get("urllib3", "0")) < Version("2.8.0")
        ):
            raise ValueError("DeepFilter must stay isolated on NumPy 1.26.4 with fixed urllib3")
    elif expected.get("pyjwt") != "2.15.1":
        raise ValueError("security candidate must install PyJWT 2.15.1")
    for item in report["install"]:
        c = components[canonical(item["metadata"]["name"])]
        info = item["download_info"]
        url = info["url"]
        parsed = urlsplit(url)
        if parsed.scheme == "file":
            if parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("local dependency URL has unexpected authority or suffix")
            local = Path(url2pathname(parsed.path)).resolve(strict=True)
            relative = local.relative_to(source.resolve(strict=True)).as_posix()
            local_sources = {
                "pipecat-ai": "third_party/pipecat",
                "kokoro-onnx": "third_party/kokoro_onnx",
                "deepfilternet": "optional/deepfilter-runtime",
            }
            if local_sources.get(canonical(c["name"])) != relative or not local.is_dir():
                raise ValueError("unexpected local dependency source")
            c.setdefault("properties", []).append({"name": "viola:local-source", "value": relative})
            c["externalReferences"] = [
                x for x in c.get("externalReferences", []) if urlsplit(x.get("url", "")).scheme.lower() != "file"
            ]
            c.pop("purl", None)
        else:
            if (
                parsed.scheme != "https"
                or parsed.hostname != "files.pythonhosted.org"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("dependency source must be an uncredentialed public PyPI artifact")
            sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
            if not sha or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("remote dependency is missing exact SHA-256")
            hashes = c.setdefault("hashes", [])
            if any(x.get("alg") == "SHA-256" and x.get("content") != sha for x in hashes):
                raise ValueError("SBOM artifact hash conflicts with installed report")
            c["hashes"] = [x for x in hashes if x.get("alg") != "SHA-256"] + [{"alg": "SHA-256", "content": sha}]
            c["externalReferences"] = [
                x for x in c.get("externalReferences", []) if x.get("type") != "distribution"
            ] + [{"type": "distribution", "url": url}]
    root_ref = bom.get("metadata", {}).get("component", {}).get("bom-ref")
    if not root_ref:
        raise ValueError("SBOM root component is absent")
    bom["dependencies"] = active_dependencies(report, components, root_ref)
    validate_graph(bom)
    return bom


def normalize_vcs_references(bom: dict) -> list[dict]:
    changes = []
    for component in bom["components"]:
        for reference in component.get("externalReferences", []):
            original = reference.get("url", "")
            match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([^\s]+)", original)
            if reference.get("type") == "vcs" and match:
                normalized = f"ssh://git@{match[1]}/{match[2]}"
                reference["url"] = normalized
                changes.append({"component": component["bom-ref"], "original": original, "normalized": normalized})
    return changes


def emit(scope: str, files: dict[str, dict]) -> None:
    raw = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > 10_000_000:
        raise ValueError("inventory evidence exceeds bounded decoded payload")
    payload = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    if len(payload) > 2_000_000:
        raise ValueError("inventory evidence exceeds bounded log payload")
    print(f"VIOLA_EVIDENCE_BEGIN {scope} {digest(raw)} {len(raw)}")
    for i in range(0, len(payload), 3000):
        print("VIOLA_EVIDENCE_DATA " + payload[i : i + 3000])
    print(f"VIOLA_EVIDENCE_END {scope}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--sbom", type=Path, required=True)
    p.add_argument("--report", type=Path)
    p.add_argument("--installed", type=Path)
    args = p.parse_args()
    manifest_path = args.source.parent / "PUBLIC_METADATA/source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_expected_source(args.source, manifest)
    bom = json.loads(args.sbom.read_text(encoding="utf-8-sig"))
    validate_graph(bom)
    receipt = {
        "scope": args.scope,
        "source_revision": manifest["revision"],
        "source_tree_sha256": manifest["sha256"],
        "manifest_sha256": digest(manifest_path.read_bytes()),
        "platform_system": platform.system(),
    }
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8-sig"))
        installed = json.loads(args.installed.read_text(encoding="utf-8-sig"))
        bom = bind_python(args.source, args.scope, report, installed, bom)
        receipt.update(
            {
                "pip_report_sha256": digest(args.report.read_bytes()),
                "input_report_sha256": report.get("input_report_sha256", {}),
                "marker_environment": report["environment"],
                "installed": installed,
                "pip_report_matches_installed": True,
                "pip_check": "passed_before_generation",
            }
        )
    else:
        package = json.loads((args.source / "ui/react-app/package.json").read_text())
        if package["overrides"]["brace-expansion"] != "5.0.12":
            raise ValueError("unexpected frontend security override")
        if not any(c.get("name") == "brace-expansion" and c.get("version") == "5.0.12" for c in bom["components"]):
            raise ValueError("SBOM is missing fixed frontend dependency")
        receipt["package_lock_sha256"] = digest((args.source / "ui/react-app/package-lock.json").read_bytes())
    normalized = normalize_vcs_references(bom)
    if normalized:
        receipt["vcs_url_normalizations"] = normalized
    props = bom.setdefault("metadata", {}).setdefault("properties", [])
    props.extend(
        {
            "name": "viola:" + key.replace("_", "-"),
            "value": json.dumps(value, sort_keys=True) if not isinstance(value, str) else value,
        }
        for key, value in receipt.items()
        if key not in {"installed", "vcs_url_normalizations"}
    )
    if args.report:
        receipt["resolved_packages"] = [
            {
                "name": row["metadata"]["name"],
                "version": row["metadata"]["version"],
                "requested": row.get("requested", False),
                "requested_extras": row.get("requested_extras", []),
                "requires_dist": row["metadata"].get("requires_dist", []),
                "source": next(
                    (
                        x["value"]
                        for x in next(
                            c for c in bom["components"] if canonical(c["name"]) == canonical(row["metadata"]["name"])
                        ).get("properties", [])
                        if x["name"] == "viola:local-source"
                    ),
                    row["download_info"]["url"],
                ),
                "sha256": row["download_info"].get("archive_info", {}).get("hashes", {}).get("sha256"),
            }
            for row in report["install"]
        ]
    payload = {"sbom": bom, "receipt": receipt}
    public_text = json.dumps(payload)
    if "file://" in public_text.lower() or str(args.source.resolve()).replace("\\", "\\\\") in public_text:
        raise ValueError("local source path leaked into public inventory")
    emit(args.scope, payload)


if __name__ == "__main__":
    main()
