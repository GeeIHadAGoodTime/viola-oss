"""HTTP rating contracts with synthetic playback and a file-backed write recorder."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import APIRouter, FastAPI

from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.rating import register_rating_routes


class RecordingRatings:
    """Record the persistence boundary without a provider, database, or user data."""

    def __init__(self, path):
        self.path = path
        self.writes = []
        self.path.write_text(json.dumps({"track-a": "disliked", "track-b": "liked"}))

    def _write(self, video_id, value):
        stored = json.loads(self.path.read_text())
        if value is None:
            stored.pop(video_id, None)
        else:
            stored[video_id] = value
        self.path.write_text(json.dumps(stored))
        self.writes.append((video_id, value))

    def thumbs_up(self, *, video_id, title, artist):
        self._write(video_id, "liked")

    def thumbs_down(self, *, video_id, title, artist):
        self._write(video_id, "disliked")

    def remove_rating(self, video_id):
        self._write(video_id, None)


class ControlledToolbox:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def record_and_call(self, callback, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await callback()


class RatingTargetContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecordingRatings(Path(self.temp.name) / "ratings.json")
        self.original_bytes = self.store.path.read_bytes()
        self.music = SimpleNamespace(current_track={"id": "track-a", "title": "Track A"})
        self.context = SimpleNamespace(router=APIRouter(), bindings=SimpleNamespace(music=self.music))
        self.toolbox = ControlledToolbox()
        self.app = FastAPI()
        self.app.dependency_overrides[require_auth] = lambda: None
        register_rating_routes(self.context, self.toolbox)
        self.app.include_router(self.context.router)
        rating_patch = patch("music.rating_system.get_rating_system", return_value=self.store)
        self.get_ratings = rating_patch.start()
        self.addCleanup(rating_patch.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://rating.test")
        self.addAsyncCleanup(self.client.aclose)

    def assert_unchanged(self):
        self.assertEqual(self.store.path.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.writes, [])

    async def test_delayed_old_track_requests_never_write_the_new_track(self):
        for value in ("liked", "disliked", None):
            with self.subTest(rating=value):
                self.music.current_track = {"id": "track-a", "title": "Track A"}
                self.toolbox.entered.clear()
                self.toolbox.release.clear()
                pending = asyncio.create_task(self.client.post("/v1/rating", json={
                    "rating": value, "expected_track_id": "track-a",
                }))
                await self.toolbox.entered.wait()
                self.music.current_track = {"id": "track-b", "title": "Track B"}
                self.toolbox.release.set()
                response = await pending
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "rating_track_changed")
                self.assert_unchanged()
        self.get_ratings.assert_not_called()

    async def test_bound_and_legacy_requests_keep_all_three_rating_operations(self):
        for bound in (False, True):
            for value in ("liked", "disliked", None):
                with self.subTest(bound=bound, rating=value):
                    body = {"rating": value}
                    if bound:
                        body["expected_track_id"] = "track-a"
                    response = await self.client.post("/v1/rating", json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"ok": True, "video_id": "track-a", "rating": value})
                    stored = json.loads(self.store.path.read_text())
                    self.assertEqual(stored.get("track-a"), value)
                    self.assertEqual(stored["track-b"], "liked")
                    self.assertEqual(self.store.writes[-1], ("track-a", value))

    async def test_video_id_precedence_and_state_fallback_match_the_target(self):
        tracks = [
            {"id": "other-id", "video_id": "track-a", "title": "Track A"},
            SimpleNamespace(id="other-id", video_id="track-a", title="Track A", artist=None),
        ]
        for track in tracks:
            for state in ({"now_playing": track}, SimpleNamespace(now_playing=track)):
                with self.subTest(track=track, state=state):
                    self.context.bindings.music = SimpleNamespace(get_state=lambda: state)
                    response = await self.client.post("/v1/rating", json={
                        "rating": "liked", "expected_track_id": "track-a",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.store.writes[-1], ("track-a", "liked"))
                    mismatch = await self.client.post("/v1/rating", json={
                        "rating": "liked", "expected_track_id": "other-id",
                    })
                    self.assertEqual(mismatch.status_code, 409, mismatch.text)

    async def test_malformed_explicit_target_cannot_fall_back_to_current_track(self):
        for target in (None, "", " ", 1, False, [], {}):
            with self.subTest(target=target):
                response = await self.client.post("/v1/rating", json={
                    "rating": "liked", "expected_track_id": target,
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_expected_track_id")
                self.assert_unchanged()
        self.get_ratings.assert_not_called()

    async def test_existing_error_codes_and_invalid_rating_precedence_are_preserved(self):
        cases = [
            (None, "liked", "music_backend_not_available"),
            (SimpleNamespace(current_track=None), "liked", "no_track_currently_playing"),
            (SimpleNamespace(current_track=object()), "liked", "could_not_get_track_info"),
            (SimpleNamespace(current_track={"title": "No ID"}), "liked", "track_has_no_video_id"),
            (self.music, "invalid", "invalid_rating_value_invalid"),
        ]
        for music, value, code in cases:
            for bound in (False, True):
                with self.subTest(code=code, bound=bound):
                    self.context.bindings.music = music
                    body = {"rating": value}
                    if bound:
                        body["expected_track_id"] = "different-track"
                    response = await self.client.post("/v1/rating", json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assert_unchanged()

    async def test_real_toolbox_preserves_conflict_envelope_without_writing(self):
        from ui.api.routes.common import RouteToolbox

        counter = Mock()
        self.context.http_requests_total = counter
        self.toolbox.record_and_call = RouteToolbox(self.context).record_and_call
        self.music.current_track = {"id": "track-b", "title": "Track B"}
        response = await self.client.post("/v1/rating", json={
            "rating": "liked", "expected_track_id": "track-a",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "rating_track_changed")
        counter.inc.assert_called_once_with(route="/v1/rating", code="409", method="POST")
        self.assert_unchanged()
        self.get_ratings.assert_not_called()

    async def test_missing_rating_retains_the_existing_clear_behavior(self):
        for body in ({}, {"expected_track_id": "track-a"}):
            response = await self.client.post("/v1/rating", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.writes[-1], ("track-a", None))
            self.assertNotIn("track-a", json.loads(self.store.path.read_text()))

    async def test_matching_target_preserves_backend_failure_response(self):
        self.get_ratings.side_effect = RuntimeError("Synthetic rating store unavailable")
        response = await self.client.post("/v1/rating", json={
            "rating": "liked", "expected_track_id": "track-a",
        })
        self.assertEqual(response.status_code, 500, response.text)
        self.assertFalse(response.json()["ok"])
        self.assert_unchanged()

    async def test_track_switch_after_snapshot_still_writes_only_the_bound_track(self):
        def switch_before_store():
            self.music.current_track = {"id": "track-b", "title": "Track B"}
            return self.store
        self.get_ratings.side_effect = switch_before_store
        response = await self.client.post("/v1/rating", json={
            "rating": "liked", "expected_track_id": "track-a",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.writes, [("track-a", "liked")])
        self.assertEqual(json.loads(self.store.path.read_text())["track-b"], "liked")


if __name__ == "__main__":
    unittest.main()
