"""Run the explicit inactive runtime controls in a fresh, inert process."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


class CJKRuntimeReconstructionTests(unittest.TestCase):
    def test_inactive_runtime_contracts_in_fresh_process(self):
        source = Path(__file__).resolve().parents[2]
        fixture = source / "tools" / "qualification" / "cjk_runtime_replay.py"
        env = dict(os.environ)
        env.update(
            VIOLA_KOKORO_PHONEMIZER="misaki-en",
            ORT_DISABLE_TELEMETRY="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1",
        )
        result = subprocess.run(
            [sys.executable, "-B", str(fixture), "-v"],
            cwd=source,
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 34 tests", result.stderr)
        self.assertIn("\nOK\n", result.stderr)


if __name__ == "__main__":
    unittest.main()
