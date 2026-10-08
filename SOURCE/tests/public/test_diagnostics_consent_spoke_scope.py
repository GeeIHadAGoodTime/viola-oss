"""Synthetic source checks for the hub's diagnostic-consent authority boundary."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("diagnostic_consent_spoke_scopes", SOURCE_ROOT / "auth/spoke_scopes.py")
scopes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scopes)


class DiagnosticConsentSpokeScopeTests(unittest.TestCase):
    def assert_consent_denied(self):
        for trusted in (False, True):
            for path in ("/v1/diagnostics/consent", "/v1/diagnostics/consent/", "/v1/diagnostics/consent/extra"):
                with self.subTest(trusted=trusted, path=path):
                    self.assertTrue(scopes.is_spoke_denied(path, trusted=trusted))
                    self.assertFalse(scopes.is_spoke_path_allowed(path, trusted=trusted))
                    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        self.assertFalse(scopes.is_spoke_allowed(path, method, trusted=trusted))

    def test_consent_is_denied_to_paired_and_cloud_spokes(self):
        self.assert_consent_denied()

    def test_paired_spoke_ordinary_diagnostics_remain_reachable(self):
        for path in (
            "/v1/diagnostics",
            "/v1/diagnostics/summary",
            "/v1/diagnostics/wake-word",
            "/v1/diagnostics/request-frontend-state",
            "/v1/diagnostics/consent-status",
        ):
            with self.subTest(path=path):
                self.assertFalse(scopes.is_spoke_denied(path, trusted=True))
                self.assertTrue(scopes.is_spoke_path_allowed(path, trusted=True))
                self.assertTrue(scopes.is_spoke_allowed(path, "GET", trusted=True))
                self.assertTrue(scopes.is_spoke_allowed(path, "POST", trusted=True))

    def test_removing_consent_prefix_reopens_the_sensitive_route(self):
        old_prefixes = tuple(p for p in scopes.SPOKE_SENSITIVE_DENIED_PREFIXES if p != "/v1/diagnostics/consent")
        with patch.object(scopes, "SPOKE_SENSITIVE_DENIED_PREFIXES", old_prefixes):
            self.assertTrue(scopes.is_spoke_allowed("/v1/diagnostics/consent", "POST", trusted=True))
            self.assertFalse(scopes.is_spoke_denied("/v1/diagnostics/consent", trusted=True))


if __name__ == "__main__":
    unittest.main()
