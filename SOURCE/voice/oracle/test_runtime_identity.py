from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("runtime_identity.py")
_SPEC = importlib.util.spec_from_file_location("runtime_identity_under_test", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
runtime_identity = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runtime_identity
_SPEC.loader.exec_module(runtime_identity)

evaluate_runtime_identity = runtime_identity.evaluate_runtime_identity
verify_runtime_identity = runtime_identity.verify_runtime_identity


def test_matching_cwd_passes(tmp_path) -> None:
    root = tmp_path / "runtime-root"
    root.mkdir()

    result = evaluate_runtime_identity(
        pid=1234,
        cwd=str(root),
        exe="C:/Python/python.exe",
        cmdline=["C:/Python/python.exe"],
        expected_root=root,
    )

    assert result.ok
    assert result.pid == 1234
    assert result.cwd == str(root)


def test_matching_cmdline_path_passes_when_cwd_is_unavailable(tmp_path) -> None:
    root = tmp_path / "runtime-root"
    root.mkdir()
    script = root / "voice" / "oracle" / "live_voice_oracle.py"

    result = evaluate_runtime_identity(
        pid=1234,
        cwd=None,
        exe="C:/Python/python.exe",
        cmdline=["C:/Python/python.exe", str(script)],
        expected_root=root,
    )

    assert result.ok
    assert result.cwd is None


def test_wrong_root_fails(tmp_path) -> None:
    expected = tmp_path / "expected"
    other = tmp_path / "other"
    expected.mkdir()
    other.mkdir()

    result = evaluate_runtime_identity(
        pid=1234,
        cwd=str(other),
        exe="C:/Python/python.exe",
        cmdline=["C:/Python/python.exe", str(other / "server.py")],
        expected_root=expected,
    )

    assert not result.ok
    assert result.reason == "process_not_from_expected_root"


def test_missing_process_fails_even_if_identity_fields_match(tmp_path) -> None:
    root = tmp_path / "runtime-root"
    root.mkdir()

    result = evaluate_runtime_identity(
        pid=None,
        cwd=str(root),
        exe="C:/Python/python.exe",
        cmdline=["C:/Python/python.exe", str(root / "server.py")],
        expected_root=root,
    )

    assert not result.ok
    assert result.reason == "missing_process"


def test_unparsable_url_port_fails_before_psutil_lookup(tmp_path) -> None:
    root = tmp_path / "runtime-root"
    root.mkdir()

    result = verify_runtime_identity("http://127.0.0.1:not-a-port", root)

    assert not result.ok
    assert result.reason == "invalid_port"
    assert result.pid is None
