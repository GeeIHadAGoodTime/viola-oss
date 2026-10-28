"""Last run's native crash reaches the crash baseline on the next launch.

The 1.0.5 build-5 laptop crashes left a faulthandler dump on the laptop and
nothing in GlitchTip. ``diagnostics.native_crash_relay`` reads earlier unclean
run logs back and sends one anonymized minimum per crashed run.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostics import native_crash_relay as relay
from diagnostics.diagnostic_minimum import build_native_crash_minimum

HEADER = """
==== VIOLA CRASH RUN (schema 1) ====
run_started_utc: 2026-10-28T17:14:36.000000Z
component: viola_qt
pid: {pid}
==== END VIOLA CRASH RUN HEADER ====
"""

# The laptop shape: a handled first-chance COM exception made faulthandler dump
# every thread, and the dumper itself faulted half-way through a frame line.
NESTED_FAULT_IN_DUMP = """Windows fatal exception: code 0x80010108

Thread 0x00007dd0 (most recent call first):
  File "C:\\Users\\someone\\AppData\\Local\\com.useviola.Viola\\current\\_internal\\threading.py", line 331 in wait
  File "C:\\Users\\someone\\AppData\\Local\\com.useviola.Viola\\current\\_internal\\threading.py", line 1002 in _bootstrap

Current thread 0x00011598 (most recent call first):
  File "C:\\Users\\someone\\AppData\\Local\\com.useviola.Viola\\current\\_internal\\viola_qt.py", line 950 in main
  File "C:\\Users\\someone\\AppData\\Local\\com.useviola.Viola\\current\\_internal\\viola_qt.py", line 968 in <module>

Thread 0x000066d4 (most recent call first):
  File "C:\\Users\\someone\\AppData\\Local\\com.useviola.Viola\\current\\_internal\\logging\\__init__.py", line 1634 in _log
  File Windows fatal exception: access violation

"""

SURVIVED_COM_EXCEPTION = """Windows fatal exception: code 0x80010108

Stack (most recent call first):
  File "C:\\x\\viola_qt.py", line 950 in main
"""

ACCESS_VIOLATION_SINGLE_THREAD = """Windows fatal exception: access violation

Stack (most recent call first):
  File "C:\\Users\\someone\\venv\\Lib\\site-packages\\sounddevice.py", line 1100 in stop
  File "C:\\Users\\someone\\app\\voice\\capture.py", line 88 in close_stream
  File "C:\\Users\\someone\\app\\viola_qt.py", line 950 in main
"""

EXIT_HANG = """
==== VIOLA EXIT WATCHDOG ARMED ====
exit_requested_utc: 2026-10-24T04:59:09.000000Z
deadline_s: 8.0
Timeout (0:00:08)!
Thread 0x00000002 (most recent call first):
  File "C:\\x\\concurrent\\futures\\thread.py", line 81 in _worker

Thread 0x00000001 (most recent call first):
  File "C:\\x\\threading.py", line 1590 in _shutdown
"""

CLEAN_END = "\n==== VIOLA CRASH RUN ENDED CLEANLY ====\nrun_ended_utc: 2026-10-28T17:20:00Z\n"


class ParseNativeCrashTests(unittest.TestCase):
    def test_fault_inside_the_dump_reports_the_dumped_exception_and_its_stack(self):
        crash = relay.parse_native_crash(HEADER.format(pid=1) + NESTED_FAULT_IN_DUMP)
        self.assertIsNotNone(crash)
        self.assertEqual(crash.error_type, "NativeCrash")
        self.assertIn("access violation while dumping Windows fatal exception: code 0x80010108", crash.error_value)
        # The faulting ("Current") thread, oldest frame first.
        self.assertEqual([f[2] for f in crash.frames], ["<module>", "main"])

    def test_a_survived_first_chance_com_exception_is_not_a_crash(self):
        self.assertIsNone(relay.parse_native_crash(HEADER.format(pid=1) + SURVIVED_COM_EXCEPTION))

    def test_single_thread_access_violation_keeps_its_stack(self):
        crash = relay.parse_native_crash(HEADER.format(pid=1) + ACCESS_VIOLATION_SINGLE_THREAD)
        self.assertEqual(crash.error_value, "Windows fatal exception: access violation")
        self.assertEqual([f[2] for f in crash.frames], ["main", "close_stream", "stop"])
        self.assertEqual(crash.frames[-1][1], 1100)

    def test_clean_exit_means_every_dump_was_survived(self):
        self.assertIsNone(relay.parse_native_crash(HEADER.format(pid=1) + ACCESS_VIOLATION_SINGLE_THREAD + CLEAN_END))

    def test_exit_watchdog_timeout_is_an_exit_hang_with_the_main_thread(self):
        crash = relay.parse_native_crash(HEADER.format(pid=1) + EXIT_HANG)
        self.assertEqual(crash.error_type, "ExitHang")
        self.assertEqual([f[2] for f in crash.frames], ["_shutdown"])

    def test_a_debug_timeout_dump_without_the_watchdog_mark_is_ignored(self):
        text = HEADER.format(pid=1) + "Timeout (0:00:30)!\nThread 0x1 (most recent call first):\n"
        self.assertIsNone(relay.parse_native_crash(text))

    def test_fatal_python_error(self):
        text = HEADER.format(pid=1) + "Fatal Python error: Segmentation fault\n\nStack (most recent call first):\n"
        self.assertEqual(relay.parse_native_crash(text).error_type, "FatalPythonError")


class NativeCrashMinimumTests(unittest.TestCase):
    def test_no_absolute_path_or_username_leaves_the_machine(self):
        crash = relay.parse_native_crash(HEADER.format(pid=1) + ACCESS_VIOLATION_SINGLE_THREAD)
        payload = build_native_crash_minimum(
            error_type=crash.error_type,
            error_value=crash.error_value,
            frames=crash.frames,
            surface="desktop_qt",
            os_info={"os_family": "Windows", "os_version": "11", "arch": "AMD64"},
        )
        flat = repr(payload)
        self.assertNotIn("someone", flat)
        self.assertNotIn("Users", flat)
        self.assertNotIn(":\\\\", flat)
        self.assertEqual(payload["report_kind"], "crash")
        self.assertEqual(payload["error_type"], "NativeCrash")
        self.assertEqual(payload["error_location"]["function"], "stop")
        self.assertEqual(payload["stack"][-1]["module"], "sounddevice.py")
        for frame in payload["stack"]:
            self.assertEqual(set(frame), {"module", "function", "lineno"})


class RelayPreviousRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sent = []
        dispatch = patch(
            "diagnostics.diagnostic_dispatch.dispatch_native_crash_diagnostic",
            side_effect=lambda **kw: self.sent.append(kw) or "queued",
        )
        dispatch.start()
        self.addCleanup(dispatch.stop)
        alive = patch("psutil.pid_exists", side_effect=lambda pid: pid == os.getpid())
        alive.start()
        self.addCleanup(alive.stop)

    def write(self, name, text, mtime):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_reports_each_crashed_run_once_and_never_the_current_or_a_clean_one(self):
        crashed = self.write(
            "viola_qt-20261028T171436Z-111.faulthandler.log", HEADER.format(pid=111) + NESTED_FAULT_IN_DUMP, 1000
        )
        self.write(
            "viola_qt-20261028T170700Z-222.faulthandler.log",
            HEADER.format(pid=222) + ACCESS_VIOLATION_SINGLE_THREAD + CLEAN_END,
            900,
        )
        self.write(
            "viola_spoke-20261028T170000Z-333.faulthandler.log", HEADER.format(pid=333) + NESTED_FAULT_IN_DUMP, 950
        )
        current = self.write(
            "viola_qt-20261028T171700Z-%d.faulthandler.log" % os.getpid(),
            HEADER.format(pid=os.getpid()) + NESTED_FAULT_IN_DUMP,
            2000,
        )

        outcomes = relay.relay_previous_native_crashes("viola_qt", current_log=current)

        self.assertEqual(outcomes, ["queued"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["error_type"], "NativeCrash")
        self.assertEqual(self.sent[0]["surface"], "desktop_qt")
        self.assertIn(relay.RELAYED_MARK, crashed.read_text(encoding="utf-8"))
        self.assertNotIn(relay.RELAYED_MARK, current.read_text(encoding="utf-8"))

        # Next launch: already reported.
        self.assertEqual(relay.relay_previous_native_crashes("viola_qt", current_log=current), [])
        self.assertEqual(len(self.sent), 1)

    def test_a_run_that_is_still_alive_is_left_alone(self):
        live = self.write(
            "viola_qt-20261028T171436Z-%d.faulthandler.log" % os.getpid(),
            HEADER.format(pid=os.getpid()) + NESTED_FAULT_IN_DUMP,
            1000,
        )
        self.assertEqual(relay.relay_previous_native_crashes("viola_qt", current_log=None, crash_dir=self.dir), [])
        self.assertNotIn(relay.RELAYED_MARK, live.read_text(encoding="utf-8"))

    def test_never_raises(self):
        with patch.object(relay, "_read_tail", side_effect=RuntimeError("boom")):
            self.write("viola_qt-1-5.faulthandler.log", HEADER.format(pid=5) + NESTED_FAULT_IN_DUMP, 1)
            self.assertEqual(relay.relay_previous_native_crashes("viola_qt", current_log=None, crash_dir=self.dir), [])


if __name__ == "__main__":
    unittest.main()
