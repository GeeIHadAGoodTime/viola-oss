"""The desktop entry point caps OpenBLAS at one thread before numpy can load.

OpenBLAS commits a ~32 MB buffer per worker thread when its DLL loads, and the
desktop loads two copies (numpy's and scipy's). Measured on a 12-thread PC:
``import numpy, scipy.linalg`` = 774 MB private bytes uncapped, 50-132 MB with
the cap. The variable is read only once, at DLL load, so the cap has to be the
first thing viola_qt.py does.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "viola_qt.py"


def _module_tree() -> ast.Module:
    return ast.parse(ENTRY.read_text(encoding="utf-8"))


def _cap_function_source() -> str:
    tree = _module_tree()
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_cap_blas_threads")
    return ast.unparse(function)


class BlasThreadCapTests(unittest.TestCase):
    def test_cap_runs_before_any_import_other_than_os_and_sys(self):
        tree = _module_tree()
        call_index = next(
            i
            for i, n in enumerate(tree.body)
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "_cap_blas_threads"
        )
        for node in tree.body[:call_index]:
            if isinstance(node, ast.ImportFrom):
                self.fail("import before the BLAS cap: from %s" % node.module)
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
                self.assertTrue(names <= {"os", "sys"}, "import before the BLAS cap: %s" % names)

    def test_cap_defaults_to_one_thread_and_keeps_an_explicit_value(self):
        source = _cap_function_source()
        for preset, expected in ((None, "1"), ("4", "4")):
            with self.subTest(preset=preset):
                environ = {} if preset is None else {"OPENBLAS_NUM_THREADS": preset}
                namespace = {"os": type("FakeOs", (), {"environ": environ})}
                exec(compile(source, str(ENTRY), "exec"), namespace)
                namespace["_cap_blas_threads"]()
                self.assertEqual(environ["OPENBLAS_NUM_THREADS"], expected)

    def test_numpy_openblas_loads_with_one_thread(self):
        try:
            import threadpoolctl  # noqa: F401
        except ImportError:
            self.skipTest("threadpoolctl is not installed")
        script = "\n".join(
            [
                "import os, json",
                _cap_function_source(),
                "_cap_blas_threads()",
                "import numpy",
                "from threadpoolctl import threadpool_info",
                "print(json.dumps([i['num_threads'] for i in threadpool_info() if i.get('internal_api') == 'openblas']))",
            ]
        )
        env = {k: v for k, v in os.environ.items() if k not in {"OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"}}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120, check=True
        )
        threads = json.loads(result.stdout.strip().splitlines()[-1])
        if not threads:
            self.skipTest("numpy in this environment does not use OpenBLAS")
        self.assertEqual(set(threads), {1})


if __name__ == "__main__":
    unittest.main()
