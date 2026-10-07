"""Synthetic desktop-session durability contracts; no real credentials or network."""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class SyntheticCipher:
    def encrypt(self, value):
        return b"synthetic:" + value

    def decrypt(self, value):
        return value.removeprefix(b"synthetic:")


class DesktopSessionPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="viola-session-durability-")
        self.root = Path(self.temp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "VIOLA_DATA_DIR": str(self.root / "data"),
                "VIOLA_CACHE_DIR": str(self.root / "cache"),
                "VIOLA_LOG_DIR": str(self.root / "logs"),
            },
        )
        self.home = patch.object(Path, "home", return_value=self.root / "home")
        self.environment.start()
        self.home.start()
        from auth import desktop_session
        from utils.enhancements import secrets

        self.sessions = desktop_session
        self.secrets = secrets
        self.store = desktop_session.DesktopSessionStore(root_dir=self.root / "profile")
        self.store._secrets = self.manager()

    def tearDown(self):
        # Windows cannot remove a temporary profile with an open log handle.
        # Retire only handlers that this fixture's disposable root owns.
        loggers = [
            logging.getLogger(),
            *[item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger)],
        ]
        for logger in loggers:
            for handler in list(logger.handlers):
                filename = getattr(handler, "baseFilename", None)
                if filename and Path(filename).resolve().is_relative_to(self.root.resolve()):
                    logger.removeHandler(handler)
                    handler.close()
        self.home.stop()
        self.environment.stop()
        self.temp.cleanup()

    def manager(self):
        # Real persistence code, with a synthetic cipher and no OS keyring access.
        manager = self.secrets.SecureSettingsManager(app_name="synthetic-only", fallback_key_file=self.root / "unused")
        manager._init_done = True
        manager._encryption_enabled = True
        manager._cipher = SyntheticCipher()
        return manager

    def payload(self, user_id="11111111-1111-4111-8111-111111111111"):
        claims = {
            "sub": user_id,
            "session_id": "22222222-2222-4222-8222-222222222222",
            "email_verified": True,
            "exp": int(time.time()) + 3600,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return {
            "access_token": "synthetic." + encoded + ".fixture",
            "refresh_token": "synthetic-refresh",
            "expires_in": 3600,
            "user": {"id": user_id, "email": "synthetic@example.com"},
        }

    def fresh_store(self):
        store = self.sessions.DesktopSessionStore(root_dir=self.root / "profile")
        store._secrets = self.manager()
        store._secrets.load_from_file(store.token_cache_path)
        return store

    def test_successful_sign_in_survives_fresh_store_readback(self):
        minted = self.store.create_session(self.payload())
        self.assertIsNotNone(minted)
        identity = self.fresh_store().current_account_identity()
        self.assertTrue(identity.signed_in)
        self.assertEqual(identity.user_id, self.payload()["user"]["id"])

    def test_failed_cache_write_cannot_publish_a_local_session(self):
        with patch.object(self.secrets, "_ensure_parent_dir", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(self.sessions.DesktopSessionError):
                self.store.create_session(self.payload())
        self.assertFalse(self.store.current_account_identity().signed_in)
        self.assertFalse(self.fresh_store().current_account_identity().signed_in)
        self.assertFalse(self.store.token_cache_path.exists())

    def test_failed_replacement_preserves_existing_account_and_cache(self):
        self.store.create_session(self.payload())
        before = self.store.token_cache_path.read_bytes()
        with patch.object(self.secrets.os, "replace", side_effect=OSError("synthetic replacement failure")):
            with self.assertRaises(self.sessions.DesktopSessionError):
                self.store.create_session(self.payload("33333333-3333-4333-8333-333333333333"))
        self.assertEqual(self.store.token_cache_path.read_bytes(), before)
        self.assertEqual(self.fresh_store().current_account_identity().user_id, self.payload()["user"]["id"])
        self.assertEqual(list(self.store.token_cache_path.parent.glob(".%s.*" % self.store.token_cache_path.name)), [])

    def test_failed_flush_preserves_existing_cache(self):
        self.store.create_session(self.payload())
        before = self.store.token_cache_path.read_bytes()
        with patch.object(self.secrets.os, "fsync", side_effect=OSError("synthetic flush failure")):
            with self.assertRaises(self.sessions.DesktopSessionError):
                self.store.create_session(self.payload())
        self.assertEqual(self.store.token_cache_path.read_bytes(), before)

    def test_existing_best_effort_callers_keep_their_contract(self):
        with patch.object(self.secrets, "_ensure_parent_dir", side_effect=OSError("synthetic write failure")):
            self.assertIsNone(self.manager().save_to_file(self.root / "ordinary-settings"))
