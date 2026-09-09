"""Checks that can be run directly inside a source distribution."""

from __future__ import annotations

import ast
import hashlib
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SourceContract(unittest.TestCase):
    def test_desktop_security_guard_imports_are_shipped(self):
        for folder in ("mcp_hub", "mcp_servers", "services/computer_use"):
            for file in (ROOT / folder).rglob("*.py"):
                tree = ast.parse(file.read_text(encoding="utf-8-sig"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("services.computer_use."):
                        module = ROOT.joinpath(*node.module.split("."))
                        assert (
                            module.with_suffix(".py").is_file() or (module / "__init__.py").is_file()
                        ), f"{file.relative_to(ROOT)} requires {node.module}"

    def test_maintained_dependency_source_inventory(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from scripts.dependencies.verify_sources import verify_sources

        assert verify_sources(ROOT) == []

    def test_setup_and_notices_are_shipped(self):
        for path in (
            "README.md",
            ".env.example",
            "LICENSE",
            "NOTICES.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/INSTALLATION.md",
            "docs/CONFIGURATION.md",
            "docs/TELEPHONY.md",
            "docs/VERIFYING.md",
            "scripts/download_models.py",
            "ui/react-app/package-lock.json",
            "LICENSES/Silero-MIT.txt",
        ):
            with self.subTest(path=path):
                assert (ROOT / path).is_file(), path

    def test_private_material_is_absent(self):
        for path in (
            "billing",
            "admin",
            "ops",
            "memory",
            "db",
            "_diag",
            "backend/webhooks/gotrue.py",
            "backend/webhooks/standard_webhook.py",
            "ui/api/routes/public_stats.py",
            "ui/api/routes/sync_bulk.py",
            "auth/desktop_gotrue_proxy.py",
            "diagnostics/diagnostic_relay.py",
            "services/payments",
            "telephony/phone_billing.py",
            "telephony/desktop_cloud_proxy.py",
            "updates/manifests",
        ):
            with self.subTest(path=path):
                assert not (ROOT / path).exists(), path

    def test_shipped_asset_rights_match_bytes(self):
        inventory = tomllib.loads((ROOT / "LICENSES/asset_inventory.toml").read_text())
        for item in inventory["assets"]:
            with self.subTest(path=item["path"]):
                assert hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]
                assert (ROOT / item["license_file"]).is_file()
                assert item["origin"]

    def test_rejected_model_dependency_cannot_be_imported_by_runtime_source(self):
        for folder in ("telephony", "third_party/pipecat/src"):
            for file in (ROOT / folder).rglob("*.py"):
                tree = ast.parse(file.read_text(encoding="utf-8-sig"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        assert not any(alias.name.split(".")[0] == "nltk" for alias in node.names), str(file)
                    elif isinstance(node, ast.ImportFrom):
                        assert (node.module or "").split(".")[0] != "nltk", str(file)


if __name__ == "__main__":
    unittest.main()
