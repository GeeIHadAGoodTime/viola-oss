"""Windows: faulthandler must dump only the faulting thread (1.0.5 build-5 crashes).

The laptop's two APPCRASHes (python311.dll 3.11.9150.1013, 0xc0000005 at
offsets 0x259cc4 and 0x259d41) are both inside python311.dll's ``dump_frame``,
the printer faulthandler uses. On Windows faulthandler is a vectored exception
handler, so it also runs for first-chance exceptions that are handled later
(COM's RPC_E_DISCONNECTED 0x80010108). With ``all_threads=True`` it walks the
other threads' frames without the GIL while they run, and a frame popped
mid-walk is an access violation inside the handler. See
``core.crash_forensics.faulthandler_all_threads``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from core import crash_forensics as cf

SOURCE_ROOT = Path(__file__).resolve().parents[2]

# Every entrypoint that turns faulthandler on for a shipped process.
ENTRYPOINTS = (
    "viola_qt.py",
    "START_WEB_INTERFACE.py",
    "services/daemon/viola_daemon.py",
    "viola_spoke.py",
)

_ENABLE_RE = re.compile(r"\bfaulthandler\.enable\(|\b_fh\.enable\(")


class FaulthandlerAllThreadsPolicyTests(unittest.TestCase):
    def test_windows_dumps_only_the_faulting_thread(self):
        with patch.object(cf.sys, "platform", "win32"):
            self.assertFalse(cf.faulthandler_all_threads())

    def test_other_platforms_keep_every_thread(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.object(cf.sys, "platform", platform):
                self.assertTrue(cf.faulthandler_all_threads())

    def test_every_entrypoint_uses_the_policy_instead_of_all_threads_true(self):
        for relative in ENTRYPOINTS:
            with self.subTest(entrypoint=relative):
                text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
                calls = [
                    line.strip()
                    for line in text.splitlines()
                    if _ENABLE_RE.search(line) and not line.lstrip().startswith("#")
                ]
                self.assertTrue(calls, "%s no longer enables faulthandler" % relative)
                for call in calls:
                    self.assertNotIn("all_threads=True", call)
                    self.assertRegex(call, r"all_threads=_?faulthandler_all_threads\(\)")


_RACE_CHILD = textwrap.dedent(
    """
    import ctypes, faulthandler, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    from core.crash_forensics import faulthandler_all_threads

    log = open(sys.argv[2], "w")
    faulthandler.enable(file=log, all_threads=faulthandler_all_threads())
    sys.setrecursionlimit(5000)
    stop = False

    def churn(depth):
        return churn(depth - 1) + 1 if depth else 0

    def worker():
        while not stop:
            churn(400)

    for _ in range(3):
        threading.Thread(target=worker, daemon=True).start()
    deadline = time.monotonic() + float(sys.argv[3])
    handled = 0
    while time.monotonic() < deadline:
        try:
            # A first-chance RPC_E_DISCONNECTED that ctypes' own SEH handler
            # catches: only faulthandler's vectored handler sees it first.
            ctypes.windll.kernel32.RaiseException(0x80010108, 0, 0, None)
        except OSError:
            handled += 1
    stop = True
    print("handled", handled, flush=True)
    """
)


@unittest.skipUnless(sys.platform == "win32", "first-chance SEH exceptions are Windows-only")
class FirstChanceExceptionDumpTests(unittest.TestCase):
    def test_handled_com_exceptions_never_fault_inside_the_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "race_child.py"
            script.write_text(_RACE_CHILD, encoding="utf-8")
            log_path = Path(tmp) / "race.faulthandler.log"
            result = subprocess.run(
                [sys.executable, str(script), str(SOURCE_ROOT), str(log_path), "4"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("handled", result.stdout)
        # The dumps happened (one per first-chance exception) ...
        self.assertIn("Windows fatal exception: code 0x80010108", log_text)
        # ... but only of the raising thread, and none of them faulted
        # half-way through a frame line (the shape of the laptop crash).
        self.assertNotIn("Thread 0x", log_text)
        self.assertIsNone(re.search(r"File [^\n]*Windows fatal exception: access violation", log_text))


if __name__ == "__main__":
    unittest.main()
