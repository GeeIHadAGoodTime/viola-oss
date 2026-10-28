"""Quitting must finish within a few seconds, whatever is stuck (1.0.5 build-5).

On the laptop a quit hung for over a minute behind a Windows passkey dialog
("Window closed: quitting Viola", then ``RuntimeError: cannot schedule new
futures after shutdown`` until force-stopped). ``arm_exit_watchdog`` puts a
deadline on the whole shutdown with faulthandler's native watchdog thread,
which needs neither the GIL nor the event loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import crash_forensics as cf

SOURCE_ROOT = Path(__file__).resolve().parents[2]

_HANGING_QUIT = textwrap.dedent(
    """
    import sys, threading, time
    sys.path.insert(0, sys.argv[1])
    from core.crash_forensics import arm_exit_watchdog

    log = open(sys.argv[2], "w")
    assert arm_exit_watchdog(1.0, file=log)
    # A second arm (aboutToQuit after the window's quit) must not push the
    # deadline out.
    assert arm_exit_watchdog(30.0, file=log)

    def stuck_worker():
        # Like a thread-pool worker blocked in a call that never returns:
        # interpreter shutdown joins non-daemon threads with no timeout.
        threading.Event().wait()

    threading.Thread(target=stuck_worker, name="stuck-worker").start()
    print("quitting", flush=True)
    sys.exit(0)
    """
)


class ExitWatchdogTests(unittest.TestCase):
    def test_a_hung_shutdown_is_ended_by_the_deadline_with_a_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "hanging_quit.py"
            script.write_text(_HANGING_QUIT, encoding="utf-8")
            log_path = Path(tmp) / "quit.faulthandler.log"
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(script), str(SOURCE_ROOT), str(log_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            elapsed = time.monotonic() - started
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("quitting", result.stdout, result.stderr[-2000:])
        # Without the watchdog this child never exits (the test would time out).
        self.assertLess(elapsed, 30.0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(cf.EXIT_WATCHDOG_MARK, log_text)
        self.assertIn("deadline_s: 1.0", log_text)
        self.assertNotIn("deadline_s: 30.0", log_text)
        self.assertIn("Timeout (0:00:01)!", log_text)
        # The dump names the thread that held the quit up.
        self.assertIn("stuck_worker", log_text)

    def test_the_desktop_arms_it_on_every_quit_and_relays_last_runs_crash(self):
        text = (SOURCE_ROOT / "viola_qt.py").read_text(encoding="utf-8")
        # aboutToQuit covers every way out, including Windows logoff.
        self.assertIn("app.aboutToQuit.connect(_arm_exit_watchdog)", text)
        # The window's own quit arms it before the event loop is even asked to stop.
        window = (SOURCE_ROOT / "ui" / "qt_native" / "webview_window.py").read_text(encoding="utf-8")
        quit_body = window.split("    def _quit_app(self):", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(quit_body.index("_arm_quit_watchdog()"), quit_body.index("QApplication.instance().quit()"))
        # Only the instance that won the single-instance lock reads old run logs.
        self.assertLess(text.index("guard.try_acquire()"), text.index('run_in_background("native_crash_relay"'))

    def test_not_armed_inside_an_in_process_test_run(self):
        with (
            patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "x::y (call)"}),
            patch.object(cf, "_EXIT_WATCHDOG_ARMED", False),
            patch("faulthandler.dump_traceback_later") as later,
        ):
            self.assertFalse(cf.arm_exit_watchdog())
        later.assert_not_called()

    def test_zero_or_negative_deadline_disables_it(self):
        with (
            patch.object(cf, "_EXIT_WATCHDOG_ARMED", False),
            patch("faulthandler.dump_traceback_later") as later,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            self.assertFalse(cf.arm_exit_watchdog(0, file=sys.__stderr__))
        later.assert_not_called()


if __name__ == "__main__":
    unittest.main()
