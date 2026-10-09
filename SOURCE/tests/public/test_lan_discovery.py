"""Socket-free regressions for the actual automatic discovery entry point."""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def _load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def discovery_source(relative="services/multiroom/discovery.py"):
    """Load whole production modules; isolate only settings, logging and I/O.

    Pytest's normal settings suppress discovery themselves. Use an installed
    runtime shape with every test/disable flag false so those guards cannot
    make a missing LAN guard pass. No application bootstrap or sockets run.
    """
    runtime = SimpleNamespace(
        api_host="127.0.0.1",
        api_port=8756,
        disable_device_discovery=False,
        test_mode=False,
        embedded_only=False,
        pytest_in_progress=False,
    )
    modules = {}
    for name, attributes in {
        "config": {"settings": runtime},
        "config.settings": {"settings": runtime},
        "core.constants": {
            "DEFAULT_API_PORT": 8756,
            "LOCALHOST": "127.0.0.1",
            "TIMEOUT_DEFAULT": 1,
            "BIND_ALL_INTERFACES": "0.0.0.0",
        },
        "core.console": {"console": Mock()},
        "core.logging_config": {"get_logger": lambda name: Mock()},
        "services.multiroom.exceptions": {"DeviceDiscoveryError": RuntimeError},
        "zeroconf": {"ServiceBrowser": Mock(), "ServiceInfo": Mock(), "ServiceListener": object, "Zeroconf": Mock()},
    }.items():
        module = ModuleType(name)
        vars(module).update(attributes)
        modules[name] = module
    with (
        patch.dict(sys.modules, modules),
        patch.object(socket, "socket", side_effect=AssertionError("real sockets are forbidden")),
        patch.object(threading.Thread, "start", side_effect=AssertionError("real threads are forbidden")),
    ):
        policy = _load("core/lan_exposure.py", "core.lan_exposure")
        module = _load(relative, "_lan_discovery_under_test")
        yield module, runtime, policy


class LanBindPolicyTests(unittest.TestCase):
    def setUp(self):
        modules = patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        self.policy = _load("core/lan_exposure.py", "_lan_policy_under_test")

    def test_local_binds_need_no_dns_and_deny_discovery(self):
        for host in (
            None,
            "",
            "  ",
            "localhost",
            "LOCALHOST.",
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
            "::1%lo",
            "::ffff:127.0.0.2",
        ):
            with (
                self.subTest(host=host),
                patch.object(self.policy.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")),
            ):
                assert self.policy.lan_listeners_allowed(host) is False

    def test_explicit_lan_and_wildcard_binds_preserve_discovery(self):
        for host in ("0.0.0.0", "::", "192.168.1.20", "fe80::1234%eth0", "::ffff:192.168.1.20"):
            with (
                self.subTest(host=host),
                patch.object(self.policy.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")),
            ):
                assert self.policy.lan_listeners_allowed(host) is True

    def test_named_binds_require_unambiguous_lan_addresses(self):
        for addresses, expected in (
            ([], False),
            (["127.0.0.2"], False),
            (["::ffff:127.0.0.2"], False),
            (["192.168.1.20"], True),
            (["192.168.1.20", "fe80::1234%eth0"], True),
            (["192.168.1.20", "127.0.0.1"], False),
            (["not-an-address"], False),
        ):
            answers = [
                (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
                for address in addresses
            ]
            with (
                self.subTest(addresses=addresses),
                patch.object(self.policy.socket, "getaddrinfo", return_value=answers) as resolver,
            ):
                assert self.policy.lan_listeners_allowed("viola-hub.example") is expected
                resolver.assert_called_once_with("viola-hub.example", None, type=socket.SOCK_STREAM)

    def test_failed_name_resolution_stays_quiet(self):
        for error in (socket.gaierror("not found"), OSError("resolver unavailable"), UnicodeError("bad name")):
            with self.subTest(error=error), patch.object(self.policy.socket, "getaddrinfo", side_effect=error):
                assert self.policy.lan_listeners_allowed("viola-hub.example") is False

    def test_invalid_resolver_address_shapes_fail_closed(self):
        for family, sockaddr in (
            (socket.AF_INET, ()),
            (socket.AF_INET, (12345, 0)),
            (socket.AF_UNSPEC, ("192.168.1.20", 0)),
        ):
            with (
                self.subTest(family=family, sockaddr=sockaddr),
                patch.object(
                    self.policy.socket, "getaddrinfo", return_value=[(family, socket.SOCK_STREAM, 6, "", sockaddr)]
                ),
            ):
                assert self.policy.lan_listeners_allowed("viola-hub.example") is False


class AutomaticDiscoveryTests(unittest.TestCase):
    def test_installed_loopback_discovery_never_starts_socket_owners(self):
        for host in (None, "", "localhost", "127.0.0.2", "::1", "::ffff:127.0.0.2"):
            with self.subTest(host=host), discovery_source() as (module, runtime, _policy):
                runtime.api_host = host
                service = module.DiscoveryService(device_id="synthetic-device", room_name="Synthetic Room")
                service._announcer.start = Mock()
                service._browser.start = Mock()
                assert service.start() is False
                assert service.is_running() is False
                service._announcer.start.assert_not_called()
                service._browser.start.assert_not_called()
                module.Zeroconf.assert_not_called()

    def test_lan_opt_in_starts_both_components_once_and_stops(self):
        for host in ("0.0.0.0", "::", "192.168.1.20"):
            with self.subTest(host=host), discovery_source() as (module, runtime, _policy):
                runtime.api_host = host
                service = module.DiscoveryService(device_id="synthetic-device", room_name="Synthetic Room")
                service._announcer.start = Mock()
                service._browser.start = Mock()
                assert service.start() is True
                assert service.start() is True
                service._announcer.start.assert_called_once_with()
                service._browser.start.assert_called_once_with()
                service.stop()
                assert service.is_running() is False

    def test_existing_disable_flags_win_without_dns(self):
        for flag in ("disable_device_discovery", "test_mode", "pytest_in_progress"):
            with self.subTest(flag=flag), discovery_source() as (module, runtime, policy):
                runtime.api_host = "viola-hub.example"
                setattr(runtime, flag, True)
                service = module.DiscoveryService(device_id="synthetic-device", room_name="Synthetic Room")
                with patch.object(policy.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
                    assert service.start() is False

    def test_unresolved_and_mixed_named_binds_do_not_start_discovery(self):
        for answers in (
            [],
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0)),
            ],
        ):
            with self.subTest(answers=answers), discovery_source() as (module, runtime, policy):
                runtime.api_host = "viola-hub.example"
                service = module.DiscoveryService(device_id="synthetic-device", room_name="Synthetic Room")
                service._announcer.start = Mock()
                service._browser.start = Mock()
                with patch.object(policy.socket, "getaddrinfo", return_value=answers):
                    assert service.start() is False
                service._announcer.start.assert_not_called()
                service._browser.start.assert_not_called()

    def test_bind_change_after_declined_start_can_be_retried(self):
        with discovery_source() as (module, runtime, _policy):
            service = module.DiscoveryService(device_id="synthetic-device", room_name="Synthetic Room")
            service._announcer.start = Mock()
            service._browser.start = Mock()
            assert service.start() is False
            runtime.api_host = "192.168.1.20"
            assert service.start() is True
            service._announcer.start.assert_called_once_with()
            service._browser.start.assert_called_once_with()
            service.stop()
