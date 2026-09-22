"""Synthetic persistence contracts for public call-history metadata."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, relative_path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, SOURCE_ROOT / relative_path)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
        raise RuntimeError("Unable to load %s" % relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _isolated_call_history_modules(temp_dir: Path) -> dict[str, types.ModuleType]:
    core_package = types.ModuleType("core")
    core_package.__path__ = []  # type: ignore[attr-defined]
    logging_module = types.ModuleType("core.logging_config")
    logging_module.get_logger = lambda _name: SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )
    platform_module = types.ModuleType("core.platform")
    platform_module.get_data_dir = lambda: temp_dir
    telephony_package = types.ModuleType("telephony")
    telephony_package.__path__ = []  # type: ignore[attr-defined]
    disclosures_module = types.ModuleType("telephony.call_disclosures")
    disclosures_module.PHONE_CALL_RETENTION_DAYS = 30
    return {
        "core": core_package,
        "core.logging_config": logging_module,
        "core.platform": platform_module,
        "telephony": telephony_package,
        "telephony.call_disclosures": disclosures_module,
    }


class CarrierHistoryOrderingContract(unittest.TestCase):
    def test_newer_carrier_revision_survives_later_metadata_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            with patch.dict(sys.modules, _isolated_call_history_modules(temp_dir)):
                history = _load_module("history_order_contract", "telephony/call_history.py")
                self.assertTrue(
                    history.update_call_history_billing_fields(
                        "call-synthetic",
                        "owner-a",
                        status="completed",
                        duration_seconds=48,
                        estimated_cost_usd=0.08,
                        carrier_revision=4,
                    )
                )
                self.assertFalse(
                    history.update_call_history_billing_fields(
                        "call-synthetic",
                        "owner-a",
                        status="stale",
                        duration_seconds=1,
                        estimated_cost_usd=0.01,
                        carrier_revision=3,
                    )
                )

                history.save_call_history(
                    history.CallHistoryEntry(
                        call_id="call-synthetic",
                        user_id="owner-a",
                        task="Synthetic task",
                        status="pending",
                        duration_seconds=2,
                        cost_breakdown={"carrier_revision": 2, "estimated_cost_usd": 0.01},
                    )
                )

                metadata_path = temp_dir / "call_history" / "call-synthetic" / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["task"], "Synthetic task")
                self.assertEqual(metadata["status"], "completed")
                self.assertEqual(metadata["duration_seconds"], 48.0)
                self.assertEqual(metadata["cost_breakdown"]["carrier_revision"], 4)
                self.assertEqual(metadata["cost_breakdown"]["estimated_cost_usd"], 0.08)
                self.assertEqual(list(metadata_path.parent.glob(".*.tmp")), [])

    def test_foreign_owner_cannot_update_or_replace_call_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            with patch.dict(sys.modules, _isolated_call_history_modules(temp_dir)):
                history = _load_module("history_owner_contract", "telephony/call_history.py")
                self.assertTrue(
                    history.update_call_history_billing_fields(
                        "call-synthetic",
                        "owner-a",
                        status="completed",
                        duration_seconds=48,
                        estimated_cost_usd=0.08,
                        carrier_revision=1,
                    )
                )

                with self.assertRaises(PermissionError):
                    history.update_call_history_billing_fields(
                        "call-synthetic",
                        "owner-b",
                        status="completed",
                        duration_seconds=49,
                        estimated_cost_usd=0.09,
                        carrier_revision=2,
                    )
                with self.assertRaises(PermissionError):
                    history.save_call_history(
                        history.CallHistoryEntry(call_id="call-synthetic", user_id="owner-b", task="Foreign task")
                    )


if __name__ == "__main__":
    unittest.main()
