"""Negative controls for the public dependency inventory evidence producer."""

import base64
import contextlib
import gzip
import io
import json
import tempfile
from pathlib import Path
import unittest

from inventory_evidence import (
    active_dependencies,
    bind_python,
    digest,
    emit,
    normalize_vcs_references,
    verify_expected_source,
)


class InventoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "version": "1",
            "environment": {"sys_platform": "win32", "python_version": "3.11", "implementation_name": "cpython"},
            "install": [
                {
                    "metadata": {"name": "PyJWT", "version": "2.15.1"},
                    "download_info": {
                        "url": "https://files.pythonhosted.org/pyjwt.whl",
                        "archive_info": {"hashes": {"sha256": "a" * 64}},
                    },
                }
            ],
        }
        self.installed = [{"name": "PyJWT", "version": "2.15.1"}]
        self.bom = {
            "metadata": {"component": {"bom-ref": "viola"}},
            "components": [{"name": "PyJWT", "version": "2.15.1", "bom-ref": "pyjwt"}],
            "dependencies": [{"ref": "pyjwt", "dependsOn": []}],
        }

    def bind(self):
        return bind_python(Path.cwd(), "windows-desktop", self.report, self.installed, self.bom)

    def test_exact_installed_graph_binds_download_hash(self):
        self.assertEqual(self.bind()["components"][0]["hashes"], [{"alg": "SHA-256", "content": "a" * 64}])

    def test_wrong_platform_rejected(self):
        self.report["environment"]["sys_platform"] = "linux"
        with self.assertRaises(ValueError):
            self.bind()

    def test_wrong_python_rejected(self):
        self.report["environment"]["python_version"] = "3.12"
        with self.assertRaises(ValueError):
            self.bind()

    def test_installed_version_mismatch_rejected(self):
        self.installed[0]["version"] = "2.13.0"
        with self.assertRaises(ValueError):
            self.bind()

    def test_extra_installed_package_rejected(self):
        self.installed.append({"name": "unreported", "version": "1"})
        with self.assertRaises(ValueError):
            self.bind()

    def test_missing_sbom_component_rejected(self):
        self.bom["components"] = []
        with self.assertRaises(ValueError):
            self.bind()

    def test_missing_download_hash_rejected(self):
        self.report["install"][0]["download_info"]["archive_info"] = {}
        with self.assertRaises(ValueError):
            self.bind()

    def test_unsafe_transport_rejected(self):
        self.report["install"][0]["download_info"]["url"] = "http://example.invalid/pkg.whl"
        with self.assertRaises(ValueError):
            self.bind()

    def test_extra_sbom_component_rejected(self):
        self.bom["components"].append({"name": "extra", "version": "1", "bom-ref": "extra"})
        with self.assertRaises(ValueError):
            self.bind()

    def test_missing_graph_rejected(self):
        self.bom["dependencies"] = []
        with self.assertRaises(ValueError):
            self.bind()

    def test_dangling_graph_rejected(self):
        self.bom["dependencies"][0]["dependsOn"] = ["absent"]
        with self.assertRaises(ValueError):
            self.bind()

    def test_tampered_source_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package-lock.json").write_bytes(b"before")
            row = {"path": "package-lock.json", "bytes": 6, "sha256": digest(b"before")}
            manifest = {"files": [row], "sha256": digest(f'{row["sha256"]}  package-lock.json\n'.encode())}
            verify_expected_source(root, manifest)
            (root / "package-lock.json").write_bytes(b"after!")
            with self.assertRaises(ValueError):
                verify_expected_source(root, manifest)

    def test_active_windows_markers_and_transitive_extras(self):
        report = {
            "environment": {"sys_platform": "win32"},
            "install": [
                {
                    "metadata": {
                        "name": "a",
                        "version": "1",
                        "requires_dist": ['b[feature]>=1; sys_platform == "win32"', 'missing; sys_platform == "linux"'],
                    },
                    "requested": True,
                },
                {"metadata": {"name": "b", "version": "1", "requires_dist": ['c; extra == "feature"']}},
                {"metadata": {"name": "c", "version": "1"}},
            ],
        }
        components = {n: {"bom-ref": n} for n in ("a", "b", "c")}
        edges = {row["ref"]: row["dependsOn"] for row in active_dependencies(report, components, "root")}
        self.assertEqual(edges, {"root": ["a"], "a": ["b"], "b": ["c"], "c": []})

    def test_unrequested_optional_edge_excluded(self):
        report = {
            "environment": {"sys_platform": "win32"},
            "install": [
                {
                    "metadata": {"name": "a", "version": "1", "requires_dist": ['b; extra == "unused"']},
                    "requested": True,
                },
                {"metadata": {"name": "b", "version": "1"}, "requested": True},
            ],
        }
        components = {n: {"bom-ref": n} for n in ("a", "b")}
        edges = {row["ref"]: row["dependsOn"] for row in active_dependencies(report, components, "root")}
        self.assertEqual(edges["a"], [])
        self.assertEqual(edges["root"], ["a", "b"])

    def test_conflicting_artifact_hash_rejected(self):
        self.bom["components"][0]["hashes"] = [{"alg": "SHA-256", "content": "b" * 64}]
        with self.assertRaises(ValueError):
            self.bind()

    def test_credentialed_artifact_url_rejected(self):
        self.report["install"][0]["download_info"]["url"] = "https://files.pythonhosted.org/pkg.whl?token=secret"
        with self.assertRaises(ValueError):
            self.bind()

    def test_file_authority_rejected(self):
        self.report["install"][0]["download_info"]["url"] = "file://untrusted-host/third_party/pipecat"
        with self.assertRaises(ValueError):
            self.bind()

    def test_deepfilter_rejects_stale_urllib3_or_mixed_desktop(self):
        with self.assertRaises(ValueError):
            bind_python(Path.cwd(), "windows-deepfilter", self.report, self.installed, self.bom)

    def test_deepfilter_accepts_isolated_fixed_graph_and_local_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            local = source / "optional/deepfilter-runtime"
            local.mkdir(parents=True)
            rows = []
            for name, version in [("DeepFilterNet", "0.5.6+viola.1"), ("numpy", "1.26.4"), ("urllib3", "2.8.0")]:
                info = {
                    "url": "https://files.pythonhosted.org/" + name + ".whl",
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                }
                if name == "DeepFilterNet":
                    info = {"url": local.as_uri(), "dir_info": {}}
                rows.append({"metadata": {"name": name, "version": version}, "download_info": info, "requested": True})
            report = {**self.report, "install": rows}
            installed = [{"name": x["metadata"]["name"], "version": x["metadata"]["version"]} for x in rows]
            bom = {
                "metadata": {"component": {"bom-ref": "viola"}},
                "components": [{**x, "bom-ref": x["name"]} for x in installed],
                "dependencies": [{"ref": x["name"], "dependsOn": []} for x in installed],
            }
            result = bind_python(source, "windows-deepfilter", report, installed, bom)
            self.assertEqual(
                result["components"][0]["properties"],
                [{"name": "viola:local-source", "value": "optional/deepfilter-runtime"}],
            )

    def test_npm_scp_vcs_url_normalized_without_changing_package_identity(self):
        bom = {
            "components": [
                {
                    "name": "example",
                    "version": "1",
                    "bom-ref": "example@1",
                    "externalReferences": [{"type": "vcs", "url": "git@github.com:owner/example.git"}],
                }
            ]
        }
        changes = normalize_vcs_references(bom)
        self.assertEqual(bom["components"][0]["externalReferences"][0]["url"], "ssh://git@github.com/owner/example.git")
        self.assertEqual(changes[0]["original"], "git@github.com:owner/example.git")
        self.assertEqual(bom["components"][0]["version"], "1")

    def test_bounded_payload_round_trip(self):
        out = io.StringIO()
        value = {"sbom": self.bom, "receipt": {"scope": "test"}}
        with contextlib.redirect_stdout(out):
            emit("test", value)
        lines = out.getvalue().splitlines()
        raw = gzip.decompress(
            base64.b64decode("".join(x.split(" ", 1)[1] for x in lines if x.startswith("VIOLA_EVIDENCE_DATA ")))
        )
        self.assertEqual(json.loads(raw), value)
        self.assertEqual(lines[0].split()[2], digest(raw))
        self.assertEqual(int(lines[0].split()[3]), len(raw))


if __name__ == "__main__":
    unittest.main()
