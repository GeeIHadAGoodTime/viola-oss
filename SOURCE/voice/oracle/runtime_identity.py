"""Runtime identity checks for local live-oracle targets."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOCALHOST_NAMES = {"127.0.0.1", "localhost", "::1"}
WILDCARD_LISTEN_ADDRESSES = {"0.0.0.0", "::", ""}  # nosec B104 - detector constants, not bind targets.


@dataclass(frozen=True)
class RuntimeIdentityResult:
    pid: int | None
    cwd: str | None
    exe: str | None
    cmdline: list[str] = field(default_factory=list)
    expected_root: str = ""
    ok: bool = False
    port: int | None = None
    reason: str = ""


def parse_base_url_port(base_url: str) -> int | None:
    parsed = _parse_base_url(base_url)
    if isinstance(parsed, RuntimeIdentityResult):
        return None
    return parsed[1]


def verify_runtime_identity(base_url: str, expected_root: str | Path) -> RuntimeIdentityResult:
    expected_root_text = _normalize_expected_root(expected_root)
    parsed = _parse_local_base_url(base_url)
    if isinstance(parsed, RuntimeIdentityResult):
        return _fail_result(expected_root_text, reason=parsed.reason)
    host, port = parsed

    try:
        import psutil
    except ImportError:
        return _fail_result(expected_root_text, port=port, reason="psutil_unavailable")

    try:
        connections = psutil.net_connections(kind="inet")
    except (OSError, psutil.Error) as exc:
        return _fail_result(expected_root_text, port=port, reason="net_connections_failed:%s" % type(exc).__name__)

    pid = _find_listening_pid(connections, host=host, port=port)
    if pid is None:
        return _fail_result(expected_root_text, port=port, reason="missing_process")

    try:
        process = psutil.Process(pid)
    except (OSError, psutil.Error) as exc:
        return _fail_result(expected_root_text, port=port, reason="process_lookup_failed:%s" % type(exc).__name__)

    cwd = _process_field(process, "cwd")
    exe = _process_field(process, "exe")
    cmdline = _process_cmdline(process)
    return evaluate_runtime_identity(
        pid=pid,
        cwd=cwd,
        exe=exe,
        cmdline=cmdline,
        expected_root=expected_root_text,
        port=port,
    )


def evaluate_runtime_identity(
    *,
    pid: int | None,
    cwd: str | None,
    exe: str | None = None,
    cmdline: Sequence[str] | None = None,
    expected_root: str | Path,
    port: int | None = None,
    reason: str = "",
) -> RuntimeIdentityResult:
    expected_root_text = _normalize_expected_root(expected_root)
    expected_root_path = Path(expected_root_text)
    cmdline_text = [str(part) for part in (cmdline or [])]
    cwd_text = str(cwd) if cwd else None
    exe_text = str(exe) if exe else None

    if pid is None:
        return RuntimeIdentityResult(
            pid=None,
            cwd=cwd_text,
            exe=exe_text,
            cmdline=cmdline_text,
            expected_root=expected_root_text,
            ok=False,
            port=port,
            reason=reason or "missing_process",
        )

    cwd_ok = _path_points_inside(cwd_text, expected_root_path)
    cmdline_ok = _cmdline_points_inside(cmdline_text, expected_root_path)
    ok = cwd_ok or cmdline_ok
    return RuntimeIdentityResult(
        pid=pid,
        cwd=cwd_text,
        exe=exe_text,
        cmdline=cmdline_text,
        expected_root=expected_root_text,
        ok=ok,
        port=port,
        reason="" if ok else reason or "process_not_from_expected_root",
    )


def _parse_local_base_url(base_url: str) -> tuple[str, int] | RuntimeIdentityResult:
    parsed = _parse_base_url(base_url)
    if isinstance(parsed, RuntimeIdentityResult):
        return parsed
    host, port = parsed
    if host not in LOCALHOST_NAMES:
        return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="non_loopback_base_url")
    return host, port


def _parse_base_url(base_url: str) -> tuple[str, int] | RuntimeIdentityResult:
    if not isinstance(base_url, str) or not base_url.strip():
        return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="invalid_base_url")
    text = base_url.strip()
    if "://" not in text:
        text = "http://%s" % text
    parsed = urlparse(text)
    host = parsed.hostname
    if not host:
        return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="invalid_base_url")
    host = host.lower()
    try:
        port = parsed.port
    except ValueError:
        return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="invalid_port")
    if port is None:
        if parsed.scheme == "http":
            port = 80
        elif parsed.scheme == "https":
            port = 443
        else:
            return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="missing_port")
    if port < 1 or port > 65535:
        return RuntimeIdentityResult(None, None, None, expected_root="", ok=False, reason="invalid_port")
    return host, port


def _find_listening_pid(connections: Sequence[Any], *, host: str, port: int) -> int | None:
    for connection in connections:
        if getattr(connection, "pid", None) is None:
            continue
        status = str(getattr(connection, "status", "")).upper()
        if status != "LISTEN":
            continue
        listen_host, listen_port = _connection_laddr(connection)
        if listen_port != port:
            continue
        if _listening_host_matches(listen_host, requested_host=host):
            return int(connection.pid)
    return None


def _connection_laddr(connection: Any) -> tuple[str, int | None]:
    laddr = getattr(connection, "laddr", None)
    if not laddr:
        return "", None
    host = getattr(laddr, "ip", None)
    port = getattr(laddr, "port", None)
    if host is not None or port is not None:
        return str(host or "").lower(), int(port) if port is not None else None
    if isinstance(laddr, Sequence) and len(laddr) >= 2:
        return str(laddr[0]).lower(), int(laddr[1])
    return "", None


def _listening_host_matches(listen_host: str, *, requested_host: str) -> bool:
    listen_host = listen_host.lower().strip("[]")
    requested_host = requested_host.lower().strip("[]")
    if listen_host in WILDCARD_LISTEN_ADDRESSES:
        return True
    if requested_host == "localhost":
        return listen_host in LOCALHOST_NAMES
    return listen_host == requested_host


def _process_field(process: Any, field_name: str) -> str | None:
    try:
        value = getattr(process, field_name)()
    except _process_info_exceptions():
        return None
    return str(value) if value else None


def _process_cmdline(process: Any) -> list[str]:
    try:
        return [str(part) for part in process.cmdline()]
    except _process_info_exceptions():
        return []


def _process_info_exceptions() -> tuple[type[BaseException], ...]:
    try:
        import psutil
    except ImportError:
        return (OSError, AttributeError)
    return (OSError, AttributeError, psutil.Error)


def _cmdline_points_inside(cmdline: Sequence[str], expected_root: Path) -> bool:
    for part in cmdline:
        for candidate in _cmdline_path_candidates(part):
            if _path_points_inside(candidate, expected_root):
                return True
    return False


def _cmdline_path_candidates(part: str) -> list[str]:
    text = part.strip().strip("\"'")
    if not text:
        return []
    candidates = [text]
    if "=" in text:
        _, value = text.split("=", 1)
        value = value.strip().strip("\"'")
        if value:
            candidates.append(value)
    return candidates


def _path_points_inside(path_text: str | None, expected_root: Path) -> bool:
    if not path_text:
        return False
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        candidate_text = os.path.normcase(os.path.abspath(str(candidate.resolve(strict=False))))
        root_text = os.path.normcase(os.path.abspath(str(expected_root.resolve(strict=False))))
        relative = os.path.relpath(candidate_text, root_text)
    except (OSError, RuntimeError, ValueError):
        return False
    return relative == "." or (relative != ".." and not relative.startswith("..%s" % os.sep))


def _normalize_expected_root(expected_root: str | Path) -> str:
    return str(Path(expected_root).expanduser().resolve(strict=False))


def _fail_result(expected_root: str, *, port: int | None = None, reason: str) -> RuntimeIdentityResult:
    return RuntimeIdentityResult(
        pid=None,
        cwd=None,
        exe=None,
        cmdline=[],
        expected_root=expected_root,
        ok=False,
        port=port,
        reason=reason,
    )


__all__ = [
    "RuntimeIdentityResult",
    "evaluate_runtime_identity",
    "parse_base_url_port",
    "verify_runtime_identity",
]
