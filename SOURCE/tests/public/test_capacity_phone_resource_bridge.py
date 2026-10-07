"""Capacity upgrades keep account-specific phone resource admission intact."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def extract_method(path, class_name, method_name, namespace):
    source = ROOT / path
    tree = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[method_name]


class CapacityPhoneResourceBridge(unittest.TestCase):
    def test_queue_precheck_passes_the_actual_account_to_the_gate(self):
        owners = []

        def concurrent(tier, user_id=None):
            owners.append((tier, user_id))
            # This customer's resource family remains Pro after capacity rises.
            return 1 if user_id == "capacity-customer" else 2

        namespace = {"get_phone_billing": lambda: SimpleNamespace(concurrent_limit_for_tier=concurrent),
                     "logger": SimpleNamespace(debug=lambda *args: None)}
        method = extract_method("telephony/call_manager.py", "CallManager", "_user_has_outbound_capacity", namespace)
        manager = SimpleNamespace(_user_outbound_call_count=lambda user_id: 1)
        self.assertFalse(method(manager, "capacity-customer", "max"))
        self.assertTrue(method(manager, "legacy-max", "max"))
        self.assertEqual(owners, [("max", "capacity-customer"), ("max", "legacy-max")])

    def test_standalone_accepts_account_keyword_without_changing_local_policy(self):
        method = extract_method("telephony/usage.py", "LocalPhoneUsage", "concurrent_limit_for_tier",
                                {"settings": SimpleNamespace(phone_global_max_concurrent=3)})
        for tier in (None, "free", "pro", "max"):
            self.assertEqual(method(None, tier), 3)
            self.assertEqual(method(None, tier, user_id="local-owner"), 3)


if __name__ == "__main__":
    unittest.main()
