"""Real file-backed stdin delivery and descendant cleanup on both native paths."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import proc_tree


class FileStdinTests(unittest.TestCase):
    def test_binary_input_is_exact_and_regular_file_without_pipe(self):
        payload = bytes(range(256)) * 1024 + b"\x00\xff\xfe end"
        result = proc_tree.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import os,stat,sys; assert stat.S_ISREG(os.fstat(0).st_mode); sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            input_data=payload,
            encoding=None,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")
        if os.name == "nt" and proc_tree._HAVE_WIN32JOB:
            self.assertEqual(result.kill_method, "win32job")

    def test_text_input_uses_selected_encoding_without_newline_conversion(self):
        text = "Gift: Café—東京\r\nsecond\n"
        result = proc_tree.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            input_data=text,
            encoding="utf-16-le",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, text)

    def test_raw_output_with_text_input_uses_utf8(self):
        text = "Account résumé 東京\x00\r\n"
        result = proc_tree.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            input_data=text,
            encoding=None,
            timeout=10,
        )
        self.assertEqual(result.stdout, text.encode("utf-8"))

    def test_empty_input_and_omitted_input_both_deliver_eof(self):
        for value in (None, b"", ""):
            with self.subTest(value=value):
                result = proc_tree.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import sys; assert sys.stdin.buffer.read()==b''; print('EOF')",
                    ],
                    input_data=value,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "EOF")

    def test_invalid_or_unencodable_input_never_launches_child(self):
        with patch.object(proc_tree, "_run_popen") as popen, patch.object(
            proc_tree, "_run_windows_job"
        ) as job:
            with self.assertRaises(TypeError):
                proc_tree.run([sys.executable, "-c", "pass"], input_data=123)
            with self.assertRaises(UnicodeEncodeError):
                proc_tree.run(
                    [sys.executable, "-c", "pass"], input_data="é", encoding="ascii"
                )
            popen.assert_not_called()
            job.assert_not_called()

    def test_portable_path_delivers_input_and_closes_private_handle_on_error(self):
        files = []
        make_file = tempfile.TemporaryFile

        def retain(*args, **kwargs):
            stream = make_file(*args, **kwargs)
            files.append(stream)
            return stream

        with patch.object(proc_tree, "_HAVE_WIN32JOB", False), patch.object(
            proc_tree.tempfile, "TemporaryFile", retain
        ):
            result = proc_tree.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ],
                input_data=b"portable\x00input",
                encoding=None,
                timeout=10,
            )
            self.assertEqual(result.stdout, b"portable\x00input")
            with self.assertRaises(OSError):
                proc_tree.run(
                    ["__viola_missing_executable__"],
                    input_data=b"private data",
                    timeout=10,
                )
        self.assertEqual(len(files), 2)
        self.assertTrue(all(stream.closed for stream in files))

    def test_stdin_rewinds_before_windows_job_fallback(self):
        payload = b"unchanged on fallback\x00\xff"

        def fail_job(*args):
            self.assertEqual(args[4]._stdin.read(), payload)
            raise OSError("synthetic native job initialization failure")

        # Only native job setup is replaced; the portable child reads the real file.
        with patch.object(proc_tree, "_IS_WINDOWS", True), patch.object(
            proc_tree, "_HAVE_WIN32JOB", True
        ), patch.object(proc_tree, "_run_windows_job", fail_job):
            result = proc_tree.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ],
                input_data=payload,
                encoding=None,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, payload)

    def _fork(self, *, timeout):
        with tempfile.TemporaryDirectory(prefix="viola-proctree-test-") as directory:
            sidecar = Path(directory) / "grandchild.pid"
            code = (
                "import os,stat,subprocess,sys,time; "
                "assert stat.S_ISREG(os.fstat(0).st_mode); "
                "data=sys.stdin.buffer.read(); "
                "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); "
                "open(sys.argv[1],'w').write(str(child.pid)); "
                "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
                + ("time.sleep(30)" if timeout else "sys.exit(0)")
            )
            began = time.monotonic()
            result = proc_tree.run(
                [sys.executable, "-B", "-c", code, str(sidecar)],
                input_data=b"forking input\x00\xff",
                encoding=None,
                timeout=2 if timeout else 10,
                kill_grace_s=2,
            )
            self.assertLess(time.monotonic() - began, 10)
            self.assertEqual(result.stdout, b"forking input\x00\xff")
            self.assertEqual(result.timed_out, timeout)
            self.assertEqual(result.returncode, 124 if timeout else 0)
            pid = int(sidecar.read_text())
            deadline = time.monotonic() + 3
            while proc_tree.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(
                proc_tree.pid_alive(pid), "grandchild survived file-stdin tree cleanup"
            )

    def test_input_timeout_reaps_grandchild_holding_inherited_files(self):
        self._fork(timeout=True)

    def test_normal_input_exit_reaps_grandchild_without_waiting_for_eof(self):
        self._fork(timeout=False)

    def test_timeout_exception_keeps_received_output(self):
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            proc_tree.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import sys,time; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stdout.flush(); time.sleep(30)",
                ],
                input_data=b"before timeout",
                encoding=None,
                timeout=1,
                raise_on_timeout=True,
            )
        self.assertEqual(raised.exception.output, b"before timeout")


if __name__ == "__main__":
    unittest.main()
