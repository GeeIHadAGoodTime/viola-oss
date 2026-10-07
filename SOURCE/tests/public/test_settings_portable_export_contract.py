"""Exercise portable settings with real file persistence and inert credentials."""

from __future__ import annotations

import importlib
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class PortableSettingsExportContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.settings_module = importlib.import_module("ui.settings_manager")
        cls.identity = importlib.import_module("core.user_context")
        assert Path(cls.settings_module.__file__).resolve() == ROOT / "ui/settings_manager.py"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(self.settings_module, "SECURE_SETTINGS_AVAILABLE", False))
        self.enterContext(self.identity.user_scope("device-portable-export-fixture"))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("No network")))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("No network")))
        self.manager = self.settings_module.SettingsManager(self.root / "settings.json")

    def export(self):
        path = self.root / "export.json"
        self.assertTrue(self.manager.export_settings(path))
        return path, json.loads(path.read_text(encoding="utf-8"))

    def write_import(self, payload):
        path = self.root / "import.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def assert_refused_without_mutation(self, payload):
        self.assertTrue(self.manager.set("theme", "dark"))
        before = dict(self.manager.settings)
        saved = self.manager.settings_file.read_bytes()
        self.assertFalse(self.manager.import_settings(self.write_import(payload)))
        self.assertEqual(dict(self.manager.settings), before)
        self.assertEqual(self.manager.settings_file.read_bytes(), saved)

    def test_fresh_default_export_imports_into_new_manager(self):
        path, _ = self.export()
        target = self.settings_module.SettingsManager(self.root / "target.json")
        self.assertTrue(target.import_settings(path))
        self.assertEqual(target.get("theme"), self.manager.get("theme"))
        self.assertIs(target.get("require_account_for_paid_actions"), True)

    def test_saved_preferences_round_trip_and_survive_reload(self):
        values = {"theme": "dark", "weather_location": "Montréal", "default_music_volume": 0}
        self.assertTrue(self.manager.update(values))
        self.manager = self.settings_module.SettingsManager(self.manager.settings_file)
        path, payload = self.export()
        target_path = self.root / "target.json"
        target = self.settings_module.SettingsManager(target_path)
        self.assertTrue(target.import_settings(path))
        reopened = self.settings_module.SettingsManager(target_path)
        for key, value in values.items():
            with self.subTest(key=key):
                self.assertIn(key, payload)
                self.assertEqual(payload[key], value)
                self.assertEqual(reopened.get(key), value)

    def test_all_existing_system_fields_are_absent_from_portable_export(self):
        for key in self.settings_module._SYSTEM_KEY_PREFIXES:
            dict.__setitem__(self.manager.settings, key, "synthetic-server-controlled")
        _, payload = self.export()
        self.assertFalse(set(payload) & self.settings_module._SYSTEM_KEY_PREFIXES)

    def test_protected_prefixes_are_absent_from_portable_export(self):
        keys = [key + "_fixture" for key in self.settings_module._SYSTEM_KEY_PREFIXES]
        for key in keys:
            dict.__setitem__(self.manager.settings, key, "synthetic-server-controlled")
        _, payload = self.export()
        self.assertFalse(set(keys) & payload.keys())

    def test_alias_classification_uses_existing_canonicalizer(self):
        aliases = self.manager.SETTING_KEY_ALIASES
        with patch.dict(aliases, {"fixture_paid_policy": "require_account_for_paid_actions"}):
            dict.__setitem__(self.manager.settings, "fixture_paid_policy", False)
            _, payload = self.export()
            self.assertNotIn("fixture_paid_policy", payload)
            self.assertNotIn("require_account_for_paid_actions", payload)

    def test_existing_ordinary_alias_is_preserved_and_import_normalizes_it(self):
        dict.__setitem__(self.manager.settings, "wake_word_sensitivity", 0.5)
        path, payload = self.export()
        self.assertEqual(payload["wake_word_sensitivity"], 0.5)
        target = self.settings_module.SettingsManager(self.root / "target.json")
        self.assertTrue(target.import_settings(path))
        self.assertEqual(target.get("wake_sensitivity"), 0.5)

    def test_export_does_not_mutate_source_memory_or_persisted_file(self):
        self.assertTrue(self.manager.set("theme", "dark"))
        before = dict(self.manager.settings)
        saved = self.manager.settings_file.read_bytes()
        _, payload = self.export()
        self.assertEqual(dict(self.manager.settings), before)
        self.assertEqual(self.manager.settings_file.read_bytes(), saved)
        expected = {
            key: value
            for key, value in before.items()
            if not self.settings_module._is_system_key(self.manager.canonicalize_setting_key(key))
        }
        self.assertEqual(payload, expected)

    def test_paid_policy_plan_and_auth_imports_remain_refused(self):
        for key, value in (
            ("require_account_for_paid_actions", False),
            ("user_plan", "premium"),
            ("billing_plan_id", "premium"),
            ("auth_enabled", False),
            ("security_auth_enabled", False),
        ):
            with self.subTest(key=key):
                self.assert_refused_without_mutation({"theme": "light", key: value})

    def test_protected_alias_import_remains_refused(self):
        with patch.dict(self.manager.SETTING_KEY_ALIASES, {"fixture_paid_policy": "require_account_for_paid_actions"}):
            self.assert_refused_without_mutation({"theme": "light", "fixture_paid_policy": False})

    def test_protected_prefix_import_remains_refused(self):
        self.assert_refused_without_mutation({"theme": "light", "require_account_for_paid_actions_fixture": False})

    def test_malformed_and_non_object_imports_remain_refused(self):
        self.assertTrue(self.manager.set("theme", "dark"))
        before = dict(self.manager.settings)
        saved = self.manager.settings_file.read_bytes()
        for value in ("not json", "[]", "null", '"text"'):
            with self.subTest(value=value):
                path = self.root / "malformed.json"
                path.write_text(value, encoding="utf-8")
                self.assertFalse(self.manager.import_settings(path))
                self.assertEqual(dict(self.manager.settings), before)
                self.assertEqual(self.manager.settings_file.read_bytes(), saved)

    def test_empty_import_still_uses_defaults(self):
        self.assertTrue(self.manager.set("theme", "dark"))
        self.assertTrue(self.manager.import_settings(self.write_import({})))
        self.assertEqual(self.manager.get("theme"), self.manager.DEFAULT_SETTINGS["theme"])
        self.assertIs(self.manager.get("require_account_for_paid_actions"), True)

    def test_export_io_failure_remains_false_without_source_mutation(self):
        before = dict(self.manager.settings)
        self.assertFalse(self.manager.export_settings(self.root / "missing" / "export.json"))
        self.assertEqual(dict(self.manager.settings), before)

    def test_export_serialization_failure_remains_false(self):
        marker = object()
        self.manager.settings["fixture_nonserializable"] = marker
        self.assertFalse(self.manager.export_settings(self.root / "invalid.json"))
        self.assertIs(self.manager.settings["fixture_nonserializable"], marker)


if __name__ == "__main__":
    unittest.main()
