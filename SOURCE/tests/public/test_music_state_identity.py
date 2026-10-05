"""Actual music-state identity boundaries with synthetic in-memory persistence."""

# ruff: noqa: PT009, PT027 -- standalone stdlib unittest coverage
from __future__ import annotations

import logging
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from music.runtime.state_service import PlayerStateService


class MusicStateIdentityTests(unittest.TestCase):
    def test_current_principal_wins_on_every_surface(self):
        for surface in ("desktop", "cloud", "unknown"):
            with (
                self.subTest(surface=surface),
                patch("config.settings.settings", SimpleNamespace(app_surface=surface)),
                patch("core.user_context.get_current_user_id", return_value="synthetic-alice"),
                patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")),
            ):
                self.assertEqual(PlayerStateService._resolve_user_id(), "synthetic-alice")

    def test_desktop_startup_retains_device_partition(self):
        with (
            patch("config.settings.settings", SimpleNamespace(app_surface="desktop")),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", return_value="device-synthetic") as device,
        ):
            self.assertEqual(PlayerStateService._resolve_user_id(), "device-synthetic")
            device.assert_called_once_with()

    def test_missing_principal_never_falls_back_on_non_desktop_surfaces(self):
        for surface in ("cloud", "unknown", "", None):
            with (
                self.subTest(surface=surface),
                patch("config.settings.settings", SimpleNamespace(app_surface=surface)),
                patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
                patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")),
            ):
                with self.assertRaisesRegex(LookupError, "user_id is required"):
                    PlayerStateService._resolve_user_id()

    def test_missing_surface_never_falls_back(self):
        with (
            patch("config.settings.settings", SimpleNamespace()),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")),
        ):
            with self.assertRaisesRegex(LookupError, "user_id is required"):
                PlayerStateService._resolve_user_id()

    def test_unavailable_configuration_never_falls_back(self):
        with (
            patch.dict(sys.modules, {"config.settings": None}),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")),
        ):
            with self.assertRaisesRegex(LookupError, "user_id is required"):
                PlayerStateService._resolve_user_id()

    def test_empty_current_identity_is_not_replaced(self):
        with (
            patch("core.user_context.get_current_user_id", return_value=""),
            patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")),
        ):
            with self.assertRaisesRegex(LookupError, "user_id is required"):
                PlayerStateService._resolve_user_id()

    def test_empty_device_identity_is_rejected(self):
        with (
            patch("config.settings.settings", SimpleNamespace(app_surface="desktop")),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", return_value=""),
        ):
            with self.assertRaisesRegex(LookupError, "user_id is required"):
                PlayerStateService._resolve_user_id()

    @staticmethod
    def service(store):
        return PlayerStateService(
            logger=logging.getLogger(__name__), default_volume=60, test_mode=False, state_store=store
        )

    def test_cloud_missing_identity_never_reads_or_writes_store(self):
        store = Mock()
        store.load_music_state.return_value = {"volume": 0}
        with (
            patch("config.settings.settings", SimpleNamespace(app_surface="cloud")),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", return_value="device-synthetic") as device,
        ):
            service = self.service(store)
            service.restore_volume_only()
            service.restore_full_state()
            service.persist()
        device.assert_not_called()
        store.load_music_state.assert_not_called()
        store.save_music_state.assert_not_called()
        self.assertEqual(service.state.volume, 60)
        self.assertFalse(service.state.is_playing)

    def test_desktop_startup_restores_zero_and_persists_to_same_partition(self):
        store = Mock()
        store.load_music_state.return_value = {"volume": 0}
        with (
            patch("config.settings.settings", SimpleNamespace(app_surface="desktop")),
            patch("core.user_context.get_current_user_id", side_effect=LookupError("no request")),
            patch("core.user_context.get_device_user_id", return_value="device-synthetic"),
        ):
            service = self.service(store)
            service.restore_volume_only()
            self.assertEqual(service.state.volume, 0)
            service.persist()
        store.load_music_state.assert_called_once_with("device-synthetic")
        self.assertEqual(store.save_music_state.call_args.kwargs["user_id"], "device-synthetic")
        self.assertEqual(store.save_music_state.call_args.kwargs["volume"], 0)

    def test_account_partitions_stay_distinct(self):
        saved = {}
        store = Mock()
        store.save_music_state.side_effect = lambda *, user_id, **payload: saved.__setitem__(user_id, payload)
        store.load_music_state.side_effect = lambda user_id: saved[user_id]
        service = self.service(store)
        with patch("core.user_context.get_device_user_id", side_effect=AssertionError("unexpected fallback")):
            for user, volume in (("synthetic-alice", 0), ("synthetic-bob", 35)):
                with patch("core.user_context.get_current_user_id", return_value=user):
                    service.set_volume(volume)
                    service.persist()
            with patch("core.user_context.get_current_user_id", return_value="synthetic-alice"):
                service.restore_volume_only()
                self.assertEqual(service.state.volume, 0)
        self.assertEqual(saved["synthetic-bob"]["volume"], 35)


if __name__ == "__main__":
    unittest.main()
