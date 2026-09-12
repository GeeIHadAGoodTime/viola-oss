"""Synthetic privacy and tenant-boundary checks for the public source core.

These checks use only fake user ids, settings, and provider tokens.  They do
not start an MCP process, contact Google or OpenAI, or write outside the test
temporary directory.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, relative_path: str) -> types.ModuleType:
    """Load a production module under a test-only name without app startup."""
    spec = importlib.util.spec_from_file_location(module_name, SOURCE_ROOT / relative_path)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
        raise RuntimeError("Unable to load %s" % relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _fake_core_logging_modules() -> dict[str, types.ModuleType]:
    core_package = types.ModuleType("core")
    core_package.__path__ = []  # type: ignore[attr-defined]
    logging_module = types.ModuleType("core.logging_config")
    logging_module.get_logger = lambda _name: SimpleNamespace(
        debug=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        exception=lambda *_a, **_k: None,
    )
    return {"core": core_package, "core.logging_config": logging_module}


class WorkspaceCredentialBoundaryContract(unittest.TestCase):
    def test_loopback_and_configuration_examples_use_synthetic_names(self) -> None:
        loopback = (SOURCE_ROOT / "telephony/loopback_phone_call_session.py").read_text(encoding="utf-8")
        schema = (SOURCE_ROOT / "config/schema.py").read_text(encoding="utf-8")
        settings = (SOURCE_ROOT / "config/settings.py").read_text(encoding="utf-8")

        self.assertIn("This is Viola calling for Alex Example.", loopback)
        self.assertIn('{"Amina": "Ah mee nah"}', schema)
        self.assertIn('{"Amina": "Ah mee nah"}', settings)

    def test_signed_in_desktop_account_is_allowed_but_foreign_user_is_not(self) -> None:
        modules = _fake_core_logging_modules()
        subprocess_module = types.ModuleType("core.subprocess_utils")
        subprocess_module.run_silent = lambda *_a, **_k: None
        modules["core.subprocess_utils"] = subprocess_module
        with patch.dict(sys.modules, modules):
            workspace_bridge = _load_module("privacy_contract_workspace_bridge", "services/oauth/workspace_bridge.py")
            signed_in_desktop_user = "00000000-0000-4000-8000-000000000001"
            user_context = types.ModuleType("core.user_context")
            user_context.get_desktop_local_principals = lambda: [signed_in_desktop_user, "device-synthetic"]
            with patch.dict(sys.modules, {"core.user_context": user_context}):
                self.assertTrue(workspace_bridge._workspace_cache_is_local_to_user(signed_in_desktop_user))
                self.assertFalse(
                    workspace_bridge._workspace_cache_is_local_to_user("00000000-0000-4000-8000-000000000002")
                )

    def test_foreign_user_cannot_export_to_fixed_workspace_cache(self) -> None:
        modules = _fake_core_logging_modules()
        subprocess_module = types.ModuleType("core.subprocess_utils")
        subprocess_module.run_silent = lambda *_a, **_k: None
        modules["core.subprocess_utils"] = subprocess_module
        with patch.dict(sys.modules, modules):
            workspace_bridge = _load_module("privacy_contract_workspace_export", "services/oauth/workspace_bridge.py")
            with patch.object(workspace_bridge, "_workspace_cache_is_local_to_user", return_value=False):
                self.assertFalse(asyncio.run(workspace_bridge.export_tokens_for_workspace("foreign-user")))

    def test_unconfigured_workspace_revocation_succeeds_without_owner_lookup(self) -> None:
        modules = _fake_core_logging_modules()
        subprocess_module = types.ModuleType("core.subprocess_utils")
        subprocess_module.run_silent = lambda *_a, **_k: None
        modules["core.subprocess_utils"] = subprocess_module
        with patch.dict(sys.modules, modules):
            workspace_bridge = _load_module("privacy_contract_workspace_clear", "services/oauth/workspace_bridge.py")
            with patch.object(workspace_bridge, "_get_workspace_mcp_root", return_value=None), patch.object(
                workspace_bridge, "_workspace_cache_is_local_to_user", return_value=False
            ):
                self.assertTrue(workspace_bridge.clear_exported_workspace_tokens("cloud-user"))

    def test_account_handoff_without_credentials_retires_old_workspace_cache(self) -> None:
        modules = _fake_core_logging_modules()
        subprocess_module = types.ModuleType("core.subprocess_utils")
        subprocess_module.run_silent = lambda *_a, **_k: None
        modules["core.subprocess_utils"] = subprocess_module
        with patch.dict(sys.modules, modules):
            workspace_bridge = _load_module("privacy_contract_workspace_handoff", "services/oauth/workspace_bridge.py")
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for filename in workspace_bridge.WORKSPACE_CREDENTIAL_FILES:
                    (root / filename).write_text("synthetic", encoding="utf-8")
                (root / workspace_bridge._WORKSPACE_CACHE_OWNER_FILE).write_text(
                    workspace_bridge._principal_fingerprint("former-account"), encoding="ascii"
                )
                credentials_module = types.ModuleType("services.oauth.credentials")

                async def no_credentials(*_args, **_kwargs):
                    return None

                credentials_module.get_google_credentials = no_credentials
                google_module = types.ModuleType("services.oauth.google")
                google_module.get_enabled_workspace_scopes = lambda: ["synthetic-scope"]
                with patch.dict(
                    sys.modules,
                    {"services.oauth.credentials": credentials_module, "services.oauth.google": google_module},
                ), patch.object(workspace_bridge, "_workspace_cache_is_local_to_user", return_value=True), patch.object(
                    workspace_bridge, "_active_desktop_principal_fingerprints", return_value={workspace_bridge._principal_fingerprint("new-account")}
                ), patch.object(workspace_bridge, "_get_workspace_mcp_root", return_value=root):
                    self.assertFalse(asyncio.run(workspace_bridge.export_tokens_for_workspace("new-account")))

                self.assertFalse(any((root / filename).exists() for filename in workspace_bridge.WORKSPACE_CREDENTIAL_FILES))

    def test_shared_cloud_runtime_does_not_launch_fixed_workspace_cache_server(self) -> None:
        modules = _fake_core_logging_modules()
        mcp_package = types.ModuleType("privacy_contract_mcp")
        mcp_package.__path__ = []  # type: ignore[attr-defined]
        launcher_module = types.ModuleType("privacy_contract_mcp.launcher")
        launcher_module._validate_server_command = lambda _config: None
        types_module = types.ModuleType("privacy_contract_mcp.types")
        types_module.ServerConfig = SimpleNamespace
        google_module = types.ModuleType("services.oauth.google")
        google_module.is_google_restricted_features_enabled = lambda _settings: True
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")
        cloud_guard_module.is_cloud_surface = lambda settings: settings.app_surface == "cloud"
        modules.update(
            {
                "privacy_contract_mcp": mcp_package,
                "privacy_contract_mcp.launcher": launcher_module,
                "privacy_contract_mcp.types": types_module,
                "services.oauth.google": google_module,
                "services.computer_use.cloud_guard": cloud_guard_module,
            }
        )
        with patch.dict(sys.modules, modules):
            runtime_config = _load_module("privacy_contract_mcp.runtime_config", "mcp_hub/runtime_config.py")
            settings = SimpleNamespace(app_surface="cloud", google_workspace_mcp_path="synthetic", api_port=8756)
            configs: list[object] = []
            runtime_config._append_google_workspace_config(configs, settings, {})

            configured_settings = SimpleNamespace(
                app_surface="cloud",
                mcp_external_servers=json.dumps(
                    [{"name": "google-workspace", "command": "node", "args": ["synthetic"]}]
                ),
            )
            runtime_config._append_configured_external_servers(configs, configured_settings)

        self.assertEqual(configs, [])


class OpenAIStorageConsentBoundaryContract(unittest.TestCase):
    def test_explicit_user_id_controls_background_storage_consent(self) -> None:
        calls: list[tuple[str, object, str | None]] = []

        class FakeSettingsManager:
            def get(self, key: str, default: object, user_id: str | None = None) -> object:
                calls.append((key, default, user_id))
                return user_id == "consenting-user"

        fake_settings_module = types.ModuleType("ui.settings_manager")
        fake_settings_module.get_settings_manager = lambda: FakeSettingsManager()
        config_settings_module = types.ModuleType("config.settings")
        config_settings_module.settings = SimpleNamespace(app_surface="desktop")
        config_package = types.ModuleType("config")
        config_package.__path__ = []  # type: ignore[attr-defined]
        services_package = types.ModuleType("services")
        services_package.__path__ = []  # type: ignore[attr-defined]
        computer_use_package = types.ModuleType("services.computer_use")
        computer_use_package.__path__ = []  # type: ignore[attr-defined]
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")

        def fail_surface_detection(_settings: object) -> bool:
            raise RuntimeError("synthetic surface lookup failure")

        cloud_guard_module.is_cloud_surface = fail_surface_detection
        modules = _fake_core_logging_modules()
        modules.update(
            {
                "ui.settings_manager": fake_settings_module,
                "config": config_package,
                "config.settings": config_settings_module,
                "services": services_package,
                "services.computer_use": computer_use_package,
                "services.computer_use.cloud_guard": cloud_guard_module,
            }
        )
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, modules):
            privacy_consent = _load_module("core.privacy_consent", "core/privacy_consent.py")
            openai_consent = _load_module("privacy_contract_openai_consent", "services/llm/openai_consent.py")
            self.assertTrue(privacy_consent.is_openai_storage_consented("consenting-user"))
            self.assertFalse(privacy_consent.is_openai_storage_consented("other-user"))
            payload = openai_consent.enforce_storage_consent({"store": True}, user_id="other-user")

        self.assertFalse(payload["store"])
        self.assertEqual([entry[2] for entry in calls], ["consenting-user", "other-user", "other-user"])

    def test_named_cloud_user_ignores_deployment_wide_storage_override(self) -> None:
        class FakeSettingsManager:
            def get(self, _key: str, _default: object, user_id: str | None = None) -> object:
                return user_id == "consenting-user"

        fake_settings_module = types.ModuleType("ui.settings_manager")
        fake_settings_module.get_settings_manager = lambda: FakeSettingsManager()
        config_settings_module = types.ModuleType("config.settings")
        config_settings_module.settings = SimpleNamespace(app_surface="cloud")
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")
        cloud_guard_module.is_cloud_surface = lambda _settings: True
        modules = _fake_core_logging_modules()
        modules.update(
            {
                "ui.settings_manager": fake_settings_module,
                "config.settings": config_settings_module,
                "services.computer_use.cloud_guard": cloud_guard_module,
            }
        )
        with patch.dict(os.environ, {"VIOLA_CONSENT_OPENAI_STORAGE": "true"}, clear=True), patch.dict(
            sys.modules, modules
        ):
            privacy_consent = _load_module("privacy_contract_cloud_storage", "core/privacy_consent.py")
            self.assertFalse(privacy_consent.is_openai_storage_consented("other-user"))
            self.assertTrue(privacy_consent.is_openai_storage_consented("consenting-user"))

    def test_unknown_surface_does_not_accept_storage_environment_override(self) -> None:
        class FakeSettingsManager:
            def get(self, _key: str, _default: object, user_id: str | None = None) -> object:
                return False

        fake_settings_module = types.ModuleType("ui.settings_manager")
        fake_settings_module.get_settings_manager = lambda: FakeSettingsManager()
        config_package = types.ModuleType("config")
        config_package.__path__ = []  # type: ignore[attr-defined]
        config_settings_module = types.ModuleType("config.settings")
        config_settings_module.settings = SimpleNamespace(app_surface="desktop")
        services_package = types.ModuleType("services")
        services_package.__path__ = []  # type: ignore[attr-defined]
        computer_use_package = types.ModuleType("services.computer_use")
        computer_use_package.__path__ = []  # type: ignore[attr-defined]
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")
        cloud_guard_module.is_cloud_surface = lambda _settings: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
        modules = _fake_core_logging_modules()
        modules.update(
            {
                "ui.settings_manager": fake_settings_module,
                "config": config_package,
                "config.settings": config_settings_module,
                "services": services_package,
                "services.computer_use": computer_use_package,
                "services.computer_use.cloud_guard": cloud_guard_module,
            }
        )
        with patch.dict(os.environ, {"VIOLA_CONSENT_OPENAI_STORAGE": "true"}, clear=True), patch.dict(
            sys.modules, modules
        ):
            privacy_consent = _load_module("privacy_contract_unknown_storage", "core/privacy_consent.py")
            self.assertFalse(privacy_consent.is_openai_storage_consented("named-user"))

    def test_ambient_cloud_user_ignores_storage_environment_override(self) -> None:
        class FakeSettingsManager:
            def get(self, _key: str, _default: object, user_id: str | None = None) -> object:
                return user_id == "ambient-consenting-user"

        fake_settings_module = types.ModuleType("ui.settings_manager")
        fake_settings_module.get_settings_manager = lambda: FakeSettingsManager()
        config_settings_module = types.ModuleType("config.settings")
        config_settings_module.settings = SimpleNamespace(app_surface="cloud")
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")
        cloud_guard_module.is_cloud_surface = lambda _settings: True
        request_context_module = types.ModuleType("core.request_context")
        request_context_module.get_request_context = lambda: SimpleNamespace(user_id="ambient-consenting-user")
        modules = _fake_core_logging_modules()
        modules.update(
            {
                "ui.settings_manager": fake_settings_module,
                "config.settings": config_settings_module,
                "services.computer_use.cloud_guard": cloud_guard_module,
                "core.request_context": request_context_module,
            }
        )
        with patch.dict(os.environ, {"VIOLA_CONSENT_OPENAI_STORAGE": "false"}, clear=True), patch.dict(
            sys.modules, modules
        ):
            privacy_consent = _load_module("privacy_contract_ambient_storage", "core/privacy_consent.py")
            self.assertTrue(privacy_consent.is_openai_storage_consented())

    def test_established_local_surface_keeps_storage_environment_override(self) -> None:
        modules = _fake_core_logging_modules()
        config_settings_module = types.ModuleType("config.settings")
        config_settings_module.settings = SimpleNamespace(app_surface="desktop")
        cloud_guard_module = types.ModuleType("services.computer_use.cloud_guard")
        cloud_guard_module.is_cloud_surface = lambda _settings: False
        modules.update(
            {"config.settings": config_settings_module, "services.computer_use.cloud_guard": cloud_guard_module}
        )
        with patch.dict(os.environ, {"VIOLA_CONSENT_OPENAI_STORAGE": "true"}, clear=True), patch.dict(
            sys.modules, modules
        ):
            privacy_consent = _load_module("privacy_contract_local_storage", "core/privacy_consent.py")
            self.assertTrue(privacy_consent.is_openai_storage_consented("local-owner"))


if __name__ == "__main__":
    unittest.main()
