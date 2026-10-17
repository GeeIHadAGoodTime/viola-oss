"""Synthetic persistence contracts for public call-history metadata."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import InvalidToken

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


class MemoryKeyRotationContract(unittest.TestCase):
    OLD_KEY = "synthetic-call-history-old-memory-key"
    CURRENT_KEY = "synthetic-call-history-current-memory-key"
    UNRELATED_KEY = "synthetic-call-history-unrelated-memory-key"
    PHONE = "+12025550100"
    TRANSCRIPT = [{"role": "assistant", "text": "Synthetic retained call transcript."}]
    FAILED = "[encrypted — decryption failed]"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_dir = Path(temporary.name)
        # Keep OS process-launch variables (including SystemRoot on Windows),
        # but never inherit real encryption material into these fixtures.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in (
            "VIOLA_MEMORY_ENCRYPTION_KEY",
            "VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS",
            "VIOLA_CALL_HISTORY_ENCRYPTION_KEY",
        ):
            os.environ.pop(name, None)
        modules = patch.dict(sys.modules, _isolated_call_history_modules(self.temp_dir))
        modules.start()
        self.addCleanup(modules.stop)
        self.history = _load_module("history_rotation_contract", "telephony/call_history.py")
        self.history._LEGACY_HISTORY_MIGRATION_DONE = True

    def _save(self, call_id="old-call", user_id="owner-a") -> Path:
        return self.history.save_call_history(
            self.history.CallHistoryEntry(
                call_id=call_id, user_id=user_id, phone_number=self.PHONE, transcript=self.TRANSCRIPT
            )
        )

    def _assert_retained_call_requires_previous(self, previous: str) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        path = self._save()
        retained = path.read_bytes()
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = previous

        entry = self.history.get_call_history("old-call")

        assert entry is not None
        assert entry.phone_number == (self.PHONE if previous == self.OLD_KEY else self.FAILED)
        assert entry.transcript == (self.TRANSCRIPT if previous == self.OLD_KEY else self.FAILED)
        # Reading old data is not a re-encryption/migration receipt.
        assert path.read_bytes() == retained

    def test_retained_call_reads_with_exact_configured_previous_key(self) -> None:
        self._assert_retained_call_requires_previous(self.OLD_KEY)

    def test_retained_call_fails_without_previous_key(self) -> None:
        self._assert_retained_call_requires_previous("")

    def test_retained_call_fails_with_unrelated_previous_key(self) -> None:
        self._assert_retained_call_requires_previous(self.UNRELATED_KEY)

    def test_new_writes_use_current_key_and_reject_previous_key(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        old_fernet = self.history._get_fernet()
        self._save()
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY
        current_fernet = self.history._get_fernet()

        stored = json.loads(self._save("new-call").read_text())
        for name in ("phone_number", "transcript"):
            assert stored[name].startswith("ENC:")
            token = stored[name][4:].encode()
            current_fernet.decrypt(token)
            try:
                old_fernet.decrypt(token)
            except InvalidToken:
                pass
            else:
                raise AssertionError("A new call record must reject the previous key")
        assert self.history.get_call_history("old-call").transcript == self.TRANSCRIPT
        assert self.history.get_call_history("new-call").transcript == self.TRANSCRIPT

    def test_fresh_process_reads_old_and_new_records_during_overlap(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        self._save()
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY
        self._save("new-call")
        code = """
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('history_contract_helpers', sys.argv[1])
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
with patch.dict(sys.modules, helpers._isolated_call_history_modules(Path(sys.argv[2]))):
    history = helpers._load_module('history_restarted_contract', 'telephony/call_history.py')
    history._LEGACY_HISTORY_MIGRATION_DONE = True
    old = history.get_call_history('old-call')
    new = history.get_call_history('new-call')
    expected = [{'role': 'assistant', 'text': 'Synthetic retained call transcript.'}]
    print(json.dumps({'old_ok': old.phone_number == '+12025550100' and old.transcript == expected,
                     'new_ok': new.phone_number == '+12025550100' and new.transcript == expected}))
"""
        environment = dict(os.environ)
        args = [sys.executable, "-c", code, str(Path(__file__).resolve()), str(self.temp_dir)]
        result = subprocess.run(
            args, cwd=SOURCE_ROOT, env=environment, capture_output=True, text=True, timeout=30, check=True
        )
        assert json.loads(result.stdout.strip().splitlines()[-1]) == {"old_ok": True, "new_ok": True}
        environment.pop("VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS")
        no_overlap = subprocess.run(
            args, cwd=SOURCE_ROOT, env=environment, capture_output=True, text=True, timeout=30, check=True
        )
        assert json.loads(no_overlap.stdout.strip().splitlines()[-1]) == {"old_ok": False, "new_ok": True}

    def test_rotation_preserves_user_scoping_and_saved_ownership(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        self._save("owner-a-call", "owner-a")
        self._save("owner-b-call", "owner-b")
        self._save("legacy-call", "")
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY

        entries = self.history.list_call_history("owner-a")
        assert [entry.call_id for entry in entries] == ["owner-a-call"]
        assert entries[0].phone_number == self.PHONE
        assert entries[0].transcript == self.TRANSCRIPT
        assert self.history.list_call_history("") == []
        assert self.history.list_call_history(None) == []
        try:
            self._save("owner-a-call", "owner-b")
        except PermissionError as exc:
            assert "owner does not match" in str(exc)
        else:
            raise AssertionError("Rotation must not allow a foreign-owner overwrite")

    def test_tampered_records_fail_even_with_correct_previous_key(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        path = self._save()
        stored = json.loads(path.read_text())
        for name in ("phone_number", "transcript"):
            token = stored[name]
            position = len(token) // 2
            stored[name] = token[:position] + ("A" if token[position] != "A" else "B") + token[position + 1 :]
        path.write_text(json.dumps(stored))
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY

        entry = self.history.get_call_history("old-call")
        assert entry.phone_number == self.FAILED
        assert entry.transcript == self.FAILED

    def test_previous_key_alone_never_enables_reads_or_writes(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        self._save()
        del os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"]
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY

        assert self.history._get_fernet() is None
        assert self.history.get_call_history("old-call").transcript == "[encrypted — key unavailable]"
        stored = json.loads(self._save("new-call").read_text())
        assert stored["phone_number"] == "***0100"
        assert stored["transcript"] == "[redacted]"

    def test_dedicated_only_key_does_not_enable_memory_overlap(self) -> None:
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.OLD_KEY
        self._save()
        del os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"]
        os.environ["VIOLA_CALL_HISTORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.OLD_KEY

        assert self.history.get_call_history("old-call").transcript == self.FAILED
        self._save("new-call")
        assert self.history.get_call_history("new-call").transcript == self.TRANSCRIPT

    def test_current_memory_key_keeps_precedence_over_dedicated_key(self) -> None:
        os.environ["VIOLA_CALL_HISTORY_ENCRYPTION_KEY"] = self.UNRELATED_KEY
        dedicated_fernet = self.history._get_fernet()
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY"] = self.CURRENT_KEY
        os.environ["VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS"] = self.CURRENT_KEY

        assert self.history._get_fernet(previous=True) is None
        stored = json.loads(self._save().read_text())
        for name in ("phone_number", "transcript"):
            try:
                dedicated_fernet.decrypt(stored[name][4:].encode())
            except InvalidToken:
                pass
            else:
                raise AssertionError("The current memory key must retain write precedence")
        assert self.history.get_call_history("old-call").transcript == self.TRANSCRIPT


if __name__ == "__main__":
    unittest.main()
