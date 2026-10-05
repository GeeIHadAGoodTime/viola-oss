"""Synthetic crash-artifact contracts; never trigger a native fault or OS hook."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import crash_forensics as cf


class CrashForensicsTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "private-module-maps"
        self.private.mkdir()
        self.log_path = self.root / "worker.faulthandler.log"
        self.modules_path = self.private / "worker.modules.json"
        self.modules = [{"base": 4096, "size": 2048, "path": "/synthetic/runtime.dll"}]
        self.real_refresher = cf._start_module_map_refresher
        self.mocks = {}
        for name, result in {
            "_utc_now": ("20261004T120000Z", "2026-10-04T12:00:00.000000Z"),
            "_resolve_session_id": "synthetic-session",
            "_app_version": "synthetic-version",
            "_git_sha": "synthetic-revision",
            "suppress_native_crash_dialogs": "synthetic-silent-noheap",
            "snapshot_loaded_modules": self.modules,
            "_start_module_map_refresher": True,
            "_prune_old_runs": 0,
            "_retention_limit": 40,
            "_open_run_log": None,
        }.items():
            context = patch.object(cf, name, return_value=result)
            self.mocks[name] = context.start()
            self.addCleanup(context.stop)
        registration = patch("atexit.register")
        self.register = registration.start()
        self.addCleanup(registration.stop)

    def open_log(self, path=None, mode="w"):
        path = path or self.log_path
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        stream = os.fdopen(descriptor, mode, encoding="utf-8")
        self.addCleanup(stream.close)
        return stream

    def install(self, stream, **overrides):
        targets = {"stream": stream, "log_path": self.log_path, "modules_path": self.modules_path}
        targets.update(overrides)
        return cf.install_crash_forensics("synthetic-worker", **targets)

    def assert_no_diagnostic_side_effects(self):
        for name in (
            "_open_run_log",
            "suppress_native_crash_dialogs",
            "snapshot_loaded_modules",
            "_start_module_map_refresher",
            "_prune_old_runs",
            "_retention_limit",
        ):
            self.mocks[name].assert_not_called()
        self.register.assert_not_called()

    def test_supplied_stream_retains_identity_header_attribution_and_clean_exit(self):
        stream = self.open_log()
        stream.write("caller-owned prefix\n")
        stream.flush()
        descriptor = stream.fileno()
        before = self.log_path.stat()
        result = self.install(stream)
        self.assertIs(result.stream, stream)
        self.assertEqual(stream.fileno(), descriptor)
        self.assertTrue(os.path.samestat(before, self.log_path.stat()))
        if os.name != "nt":
            self.assertEqual(self.log_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.log_path, self.log_path)
        self.assertEqual(result.modules_path, self.modules_path)
        self.assertEqual(result.module_count, 1)
        self.assertEqual(result.notes, [])
        self.mocks["_open_run_log"].assert_not_called()
        self.mocks["_retention_limit"].assert_not_called()
        self.mocks["_prune_old_runs"].assert_not_called()
        self.mocks["suppress_native_crash_dialogs"].assert_called_once_with()
        self.mocks["_start_module_map_refresher"].assert_called_once_with(
            self.modules_path,
            component="synthetic-worker",
            session_id="synthetic-session",
            initial_count=1,
        )
        self.assertFalse(cf._sidecar_for(self.log_path).exists())
        payload = json.loads(self.modules_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["modules"], self.modules)
        self.assertEqual(payload["captured_at"], "install")
        header = self.log_path.read_text(encoding="utf-8")
        for text in (
            "caller-owned prefix\n",
            "VIOLA CRASH RUN (schema 1)",
            "run_started_utc: 2026-10-04T12:00:00.000000Z",
            "component: synthetic-worker",
            "session_id: synthetic-session",
            "git_sha: synthetic-revision",
            "native_dialog_suppression: synthetic-silent-noheap",
            "module_map: " + str(self.modules_path),
        ):
            self.assertIn(text, header)
        self.assertNotIn(cf._CLEAN_EXIT_MARK, header)
        self.register.assert_called_once()
        self.register.call_args.args[0]()
        self.assertIn(cf._CLEAN_EXIT_MARK, self.log_path.read_text(encoding="utf-8"))
        self.assertFalse(stream.closed)

    def test_default_allocation_sidecar_and_retention_are_unchanged(self):
        stream = self.open_log()
        self.mocks["_open_run_log"].return_value = (stream, self.log_path, ["allocator-note"])
        result = cf.install_crash_forensics("synthetic-worker")
        self.mocks["_open_run_log"].assert_called_once_with("synthetic-worker", "20261004T120000Z")
        self.mocks["_retention_limit"].assert_called_once_with()
        self.mocks["_prune_old_runs"].assert_called_once_with(self.root, "synthetic-worker", 40)
        self.assertEqual(result.notes, ["allocator-note"])
        self.assertEqual(result.modules_path, cf._sidecar_for(self.log_path))
        self.assertTrue(result.modules_path.is_file())
        self.assertFalse(self.modules_path.exists())

    def test_standard_text_file_wrapper_is_accepted_without_reopening(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root) as stream:
            result = self.install(stream, log_path=Path(stream.name))
            self.assertIs(result.stream, stream)
            self.assertFalse(stream.closed)
            self.assertEqual(result.log_path, Path(stream.name))
            self.assertEqual(result.modules_path, self.modules_path)
            self.mocks["_open_run_log"].assert_not_called()

    def test_relative_target_stays_bound_when_cwd_changes_before_refresh(self):
        stream = self.open_log()
        self.mocks["_start_module_map_refresher"].side_effect = self.real_refresher
        changed_cwd = self.root / "later-working-directory"
        changed_cwd.mkdir()
        (changed_cwd / self.private.name).mkdir()
        with (
            patch.dict(os.environ, {cf._REFRESH_DISABLE_ENV: "0", cf._REFRESH_SCHEDULE_ENV: "1"}),
            patch.object(cf.threading, "Thread") as thread,
            patch.object(cf._REFRESH_STOP, "wait", return_value=False),
        ):
            with chdir(self.root):
                result = self.install(
                    stream,
                    log_path=Path(self.log_path.name),
                    modules_path=Path(self.private.name) / self.modules_path.name,
                )
            self.mocks["snapshot_loaded_modules"].return_value = self.modules + [
                {"base": 8192, "size": 10, "path": "late"}
            ]
            with chdir(changed_cwd):
                thread.call_args.kwargs["target"]()
        self.assertEqual(result.log_path, self.log_path)
        self.assertEqual(result.modules_path, self.modules_path)
        self.assertEqual(json.loads(self.modules_path.read_text(encoding="utf-8"))["captured_at"], "refresh-1")
        self.assertEqual(list((changed_cwd / self.private.name).iterdir()), [])

    def test_default_allocation_failure_propagates_before_other_hooks(self):
        self.mocks["_open_run_log"].side_effect = OSError("no writable crash-log location")
        with self.assertRaisesRegex(OSError, "no writable"):
            cf.install_crash_forensics("synthetic-worker")
        self.mocks["suppress_native_crash_dialogs"].assert_not_called()
        self.register.assert_not_called()

    def test_all_partial_target_combinations_fail_before_hooks(self):
        stream = self.open_log()
        targets = {"stream": stream, "log_path": self.log_path, "modules_path": self.modules_path}
        names = list(targets)
        for mask in range(1, 7):
            with self.subTest(mask=mask), self.assertRaisesRegex(ValueError, "supplied together"):
                cf.install_crash_forensics(
                    "synthetic-worker",
                    **{name: targets[name] for bit, name in enumerate(names) if mask & (1 << bit)},
                )
        self.assert_no_diagnostic_side_effects()
        self.assertFalse(stream.closed)

    def test_closed_read_only_binary_and_non_file_streams_are_rejected(self):
        stream = self.open_log()
        stream.close()
        with self.log_path.open("r", encoding="utf-8") as read_only, self.log_path.open("ab") as binary:
            for target in (stream, read_only, binary, io.StringIO(), object()):
                with self.subTest(target=type(target).__name__), self.assertRaises((TypeError, ValueError, OSError)):
                    self.install(target)
        self.assert_no_diagnostic_side_effects()

    def test_mismatched_or_missing_log_path_fails_without_reallocation(self):
        stream = self.open_log()
        other = self.root / "other.log"
        other.write_text("preserve", encoding="utf-8")
        for path in (other, self.root / "missing.log"):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                self.install(stream, log_path=path)
        self.assertEqual(other.read_text(encoding="utf-8"), "preserve")
        self.assert_no_diagnostic_side_effects()

    def test_non_regular_file_descriptor_is_rejected(self):
        with open(os.devnull, "w", encoding="utf-8") as stream:
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.install(stream, log_path=Path(os.devnull))
        self.assert_no_diagnostic_side_effects()

    def test_log_symlink_is_rejected(self):
        stream = self.open_log()
        link = self.root / "linked.log"
        try:
            link.symlink_to(self.log_path)
        except OSError as error:
            self.skipTest("symlinks unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "without a symlink"):
            self.install(stream, log_path=link)
        self.assert_no_diagnostic_side_effects()

    def test_module_map_and_temp_path_must_not_alias_the_log(self):
        for use_temp in (False, True):
            for alias in ("same-name", "hard-link", "symlink"):
                with self.subTest(use_temp=use_temp, alias=alias):
                    directory = self.root / (alias + str(use_temp))
                    directory.mkdir()
                    modules = directory / "map.json"
                    temporary = modules.with_name(modules.name + ".tmp%d" % os.getpid())
                    target = temporary if use_temp else modules
                    log = target if alias == "same-name" else directory / "log.txt"
                    stream = self.open_log(log)
                    if alias != "same-name":
                        try:
                            if alias == "hard-link":
                                os.link(log, target)
                            else:
                                target.symlink_to(log)
                        except OSError:
                            continue  # Windows accounts may not be allowed to create symlinks.
                    with self.assertRaises(ValueError):
                        self.install(stream, log_path=log, modules_path=modules)
                    self.assertEqual(log.read_text(encoding="utf-8"), "")
        self.assert_no_diagnostic_side_effects()

    def test_directory_or_symlink_sidecar_targets_fail_before_any_writes(self):
        stream = self.open_log()
        victim = self.private / "unrelated.txt"
        victim.write_text("preserve", encoding="utf-8")
        for use_temp in (False, True):
            for kind in ("directory", "symlink"):
                with self.subTest(use_temp=use_temp, kind=kind):
                    modules = self.private / (kind + str(use_temp) + ".json")
                    temporary = modules.with_name(modules.name + ".tmp%d" % os.getpid())
                    target = temporary if use_temp else modules
                    if kind == "directory":
                        target.mkdir()
                    else:
                        try:
                            target.symlink_to(victim)
                        except OSError:
                            continue
                    with self.assertRaisesRegex(ValueError, "regular files without symlinks"):
                        self.install(stream, modules_path=modules)
                    self.assertEqual(victim.read_text(encoding="utf-8"), "preserve")
                    if not use_temp:
                        self.assertFalse(temporary.exists())
                    if kind == "directory":
                        self.assertEqual(list(target.iterdir()), [])
        self.assert_no_diagnostic_side_effects()

    def test_missing_private_directory_is_not_created_or_redirected(self):
        stream = self.open_log()
        missing = self.root / "missing-private"
        with self.assertRaisesRegex(ValueError, "parent must already exist"):
            self.install(stream, modules_path=missing / "map.json")
        self.assertFalse(missing.exists())
        self.assert_no_diagnostic_side_effects()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO targets require POSIX")
    def test_fifo_sidecar_targets_are_rejected_before_any_write_can_block(self):
        stream = self.open_log()
        for use_temp in (False, True):
            with self.subTest(use_temp=use_temp):
                modules = self.private / ("fifo" + str(use_temp) + ".json")
                target = modules.with_name(modules.name + ".tmp%d" % os.getpid()) if use_temp else modules
                os.mkfifo(target)
                with self.assertRaisesRegex(ValueError, "regular files without symlinks"):
                    self.install(stream, modules_path=modules)
        self.assert_no_diagnostic_side_effects()

    def test_sidecar_hard_links_to_unrelated_files_are_rejected(self):
        stream = self.open_log()
        victim = self.private / "unrelated.txt"
        victim.write_text("preserve", encoding="utf-8")
        for use_temp in (False, True):
            with self.subTest(use_temp=use_temp):
                modules = self.private / ("linked" + str(use_temp) + ".json")
                target = modules.with_name(modules.name + ".tmp%d" % os.getpid()) if use_temp else modules
                try:
                    os.link(victim, target)
                except OSError as error:
                    self.skipTest("hard links unavailable: " + str(error))
                with self.assertRaisesRegex(ValueError, "multiple hard links"):
                    self.install(stream, modules_path=modules)
                self.assertEqual(victim.read_text(encoding="utf-8"), "preserve")
        self.assert_no_diagnostic_side_effects()

    def test_sidecar_write_failure_is_not_redirected_and_does_not_close_stream(self):
        stream = self.open_log()
        with patch.object(cf, "_write_module_map", side_effect=OSError("synthetic write failure")) as write:
            result = self.install(stream)
        self.assertEqual(write.call_args.args[0], self.modules_path)
        self.assertIsNone(result.modules_path)
        self.assertEqual(result.notes, ["module-map-unwritten:OSError"])
        self.mocks["_start_module_map_refresher"].assert_not_called()
        self.mocks["_open_run_log"].assert_not_called()
        self.assertFalse(cf._sidecar_for(self.log_path).exists())
        self.assertFalse(stream.closed)
        self.assertIn("module-map-unwritten:OSError", self.log_path.read_text(encoding="utf-8"))

    def test_empty_snapshot_does_not_start_refresher_or_fallback_map(self):
        self.mocks["snapshot_loaded_modules"].return_value = []
        result = self.install(self.open_log())
        self.assertIsNone(result.modules_path)
        self.assertEqual(result.module_count, 0)
        self.assertFalse(self.modules_path.exists())
        self.mocks["_start_module_map_refresher"].assert_not_called()
        self.mocks["_open_run_log"].assert_not_called()

    def test_optional_hook_failures_remain_observable(self):
        stream = self.open_log()
        self.mocks["suppress_native_crash_dialogs"].side_effect = OSError("synthetic")
        self.mocks["_start_module_map_refresher"].side_effect = RuntimeError("synthetic")
        self.register.side_effect = RuntimeError("synthetic")
        result = self.install(stream)
        self.assertEqual(result.dialog_suppression, "error:OSError")
        self.assertEqual(
            result.notes,
            [
                "dialog-suppression-failed",
                "module-refresh-unstarted",
                "clean-exit-marker-unregistered",
            ],
        )
        self.assertFalse(stream.closed)
        self.mocks["_open_run_log"].assert_not_called()

    def test_header_write_failure_is_recorded_without_fallback(self):
        stream = self.open_log()
        with patch.object(stream, "write", side_effect=OSError("synthetic header failure")):
            result = self.install(stream)
        self.assertEqual(result.notes, ["header-unwritten"])
        self.assertIs(result.stream, stream)
        self.assertFalse(stream.closed)
        self.mocks["_open_run_log"].assert_not_called()

    def test_disabled_refresher_is_recorded_without_losing_sidecar(self):
        self.mocks["_start_module_map_refresher"].return_value = False
        result = self.install(self.open_log())
        self.assertEqual(result.notes, ["module-refresh-off"])
        self.assertEqual(result.modules_path, self.modules_path)
        self.assertTrue(self.modules_path.exists())

    def test_closed_stream_at_clean_exit_is_harmless(self):
        stream = self.open_log()
        self.install(stream)
        stream.close()
        self.register.call_args.args[0]()


class CrashForensicsPlatformTests(unittest.TestCase):
    def test_windows_suppression_uses_noheap_and_preserves_error_mode(self):
        import ctypes

        previous_mode = 0x4000
        kernel = SimpleNamespace(SetErrorMode=Mock(return_value=previous_mode))
        wer = SimpleNamespace(WerSetFlags=Mock(), WerGetFlags=Mock(), GetCurrentProcess=Mock(return_value=123))

        def get_flags(process, output):
            output._obj.value = cf._WER_FAULT_REPORTING_FLAG_QUEUE | cf._WER_FAULT_REPORTING_FLAG_NOHEAP

        wer.WerGetFlags.side_effect = get_flags
        with (
            patch.object(cf.sys, "platform", "win32"),
            patch.dict(os.environ, {cf._KEEP_DIALOG_ENV: "0"}),
            patch.object(ctypes, "WinDLL", side_effect=[wer, kernel], create=True) as load_library,
            patch.object(ctypes, "HRESULT", ctypes.c_long, create=True),
        ):
            self.assertEqual(cf.suppress_native_crash_dialogs(), "wer-silent+seterrormode")
        wer.WerSetFlags.assert_called_once_with(
            cf._WER_FAULT_REPORTING_FLAG_NOHEAP
            | cf._WER_FAULT_REPORTING_FLAG_QUEUE
            | cf._WER_FAULT_REPORTING_FLAG_DISABLE_THREAD_SUSPENSION,
        )
        self.assertEqual([call.args[0] for call in load_library.call_args_list], ["kernel32", "kernel32"])
        self.assertEqual(kernel.SetErrorMode.call_args.args[0], previous_mode | 0x0001 | 0x8000)

    def test_windows_wer_failure_uses_heap_free_existing_fallback(self):
        import ctypes

        kernel = SimpleNamespace(SetErrorMode=Mock(return_value=0))
        with (
            patch.object(cf.sys, "platform", "win32"),
            patch.dict(os.environ, {cf._KEEP_DIALOG_ENV: "0"}),
            patch.object(ctypes, "WinDLL", side_effect=[OSError("synthetic WER failure"), kernel], create=True),
        ):
            self.assertEqual(cf.suppress_native_crash_dialogs(), "seterrormode-nogpfault")
        self.assertEqual(kernel.SetErrorMode.call_args.args[0], 0x0001 | 0x0002 | 0x8000)

    def test_refresher_is_bounded_and_keeps_explicit_private_target(self):
        private_target = Path("synthetic-private") / "map.json"
        samples = [[{"base": index}] for index in range(2)]
        samples[1].append({"base": 2})
        with (
            patch.dict(os.environ, {cf._REFRESH_DISABLE_ENV: "0", cf._REFRESH_SCHEDULE_ENV: "1,2"}),
            patch.object(cf.threading, "Thread") as thread,
            patch.object(cf._REFRESH_STOP, "wait", return_value=False) as wait,
            patch.object(cf, "snapshot_loaded_modules", side_effect=samples) as snapshot,
            patch.object(cf, "_write_module_map") as write,
        ):
            self.assertTrue(
                cf._start_module_map_refresher(
                    private_target,
                    component="synthetic-worker",
                    session_id="session",
                    initial_count=1,
                )
            )
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["daemon"])
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs["target"]()
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(snapshot.call_count, 2)
        write.assert_called_once_with(
            private_target,
            component="synthetic-worker",
            session_id="session",
            captured_at="refresh-2",
            modules=samples[1],
        )


class CrashForensicsDefaultAllocationTests(unittest.TestCase):
    def test_default_allocator_preserves_ordered_fallbacks_and_append_mode(self):
        import builtins

        original_open = builtins.open
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            fallback = root / "temp"
            candidates = [logs / "crash", logs, fallback / "viola-crash"]
            for fail_count in range(3):
                attempts = []

                def attempt(path, mode, **kwargs):
                    attempts.append((path, mode, kwargs))
                    if len(attempts) <= fail_count:
                        raise OSError("synthetic allocation failure")
                    return original_open(path, mode, **kwargs)

                with (
                    self.subTest(fail_count=fail_count),
                    patch.dict("sys.modules", {"core.platform": SimpleNamespace(get_logs_dir=lambda: logs)}),
                    patch("tempfile.gettempdir", return_value=str(fallback)),
                    patch("builtins.open", side_effect=attempt),
                ):
                    stream, path, notes = cf._open_run_log("synthetic-worker", "20261004T120000Z")
                    try:
                        self.assertEqual(path.parent, candidates[fail_count])
                        self.assertEqual(
                            path.name, "synthetic-worker-20261004T120000Z-%d.faulthandler.log" % os.getpid()
                        )
                        self.assertEqual([item[0].parent for item in attempts], candidates[: fail_count + 1])
                        self.assertTrue(
                            all(item[1:] == ("a", {"encoding": "utf-8", "errors": "replace"}) for item in attempts)
                        )
                        self.assertEqual(notes, ["unwritable:" + target.name for target in candidates[:fail_count]])
                    finally:
                        stream.close()

    def test_default_retention_keeps_other_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "worker-20261001.faulthandler.log"
            new = root / "worker-20261002.faulthandler.log"
            unrelated = root / "another-worker-20261001.faulthandler.log"
            for path in (old, new, unrelated):
                path.write_text("synthetic", encoding="utf-8")
                cf._sidecar_for(path).write_text("{}", encoding="utf-8")
            self.assertEqual(cf._prune_old_runs(root, "worker", keep=1), 2)
            self.assertFalse(old.exists())
            self.assertFalse(cf._sidecar_for(old).exists())
            for path in (new, unrelated):
                self.assertTrue(path.exists())
                self.assertTrue(cf._sidecar_for(path).exists())


if __name__ == "__main__":
    unittest.main()
