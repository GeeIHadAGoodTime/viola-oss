"""Run exact ordinary-package composition and inert Kokoro controls in isolation."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class CustomerRomanceCompositionTests(unittest.TestCase):
    def test_explicit_romance_composition_in_fresh_process(self):
        source = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment.update(
            VIOLA_KOKORO_PHONEMIZER="misaki-en",
            ORT_DISABLE_TELEMETRY="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1",
        )
        result = subprocess.run(
            [sys.executable, "-B", str(source / "tools/qualification/romance_runtime_replay.py"), "-v"],
            cwd=source,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 16 tests", result.stderr)
        self.assertIn("\nOK\n", result.stderr)


if __name__ == "__main__":
    unittest.main()
