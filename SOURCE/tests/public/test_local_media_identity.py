"""Source-level local-media matching and recovery with inert runtime boundaries.

The real handlers run, but search, local index and player HTTP/state are
synthetic. No model, decoder, device, provider or actual playback is invoked.
"""

from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def _load_source():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from intent.command_executor import CommandExecutor
    from intent.tool_types import ToolResult
    from intent.tools import media_tools, music_tools
    from music.local_file_identity import local_file_identity

    return CommandExecutor, ToolResult, media_tools, music_tools, local_file_identity


CommandExecutor, ToolResult, media_tools, music_tools, local_file_identity = _load_source()

PATH = r"C:\Users\synthetic\Documents\Codex\qa1482-media-20261007\qa1482-independent-tone-a-45s.wav"
OTHER = PATH.replace("tone-a-", "tone-b-")
TITLE = "Qa1482 Independent Tone A 45S"


def classify(query, **track):
    return CommandExecutor._classify_query_match(query, {"now_playing": track})


class LocalMediaIdentity(unittest.TestCase):
    def test_installed_short_path_and_display_title_match_same_file(self):
        self.assertEqual(classify(PATH, title=TITLE, artist="Unknown Artist", url=PATH, provider="local"), "exact")

    def test_windows_separators_case_and_file_uri_preserve_identity(self):
        query = r"C:\Music\A Song.wav"
        for actual in ("c:/music/a song.wav", "file:///C:/Music/A%20Song.wav", "file://localhost/C:/Music/A%20Song.wav"):
            with self.subTest(actual=actual):
                self.assertEqual(classify(query, url=actual), "exact")

    def test_unc_host_and_share_are_part_of_identity(self):
        query = r"\\server\share\A Song.wav"
        self.assertEqual(classify(query, url="file://SERVER/share/A%20Song.wav"), "exact")
        self.assertEqual(classify(query, url=r"\\other\share\A Song.wav"), "fallback_unrelated")
        self.assertEqual(classify(query, url=r"\\server\other\A Song.wav"), "fallback_unrelated")

    def test_posix_paths_preserve_case_and_file_uri_escaping(self):
        self.assertEqual(classify("/music/A Song.wav", url="file:///music/A%20Song.wav"), "exact")
        self.assertEqual(classify("/music/A Song.wav", url="/music/a song.wav"), "fallback_unrelated")

    def test_plain_filename_percent_escapes_are_not_decoded(self):
        query = r"C:\Music\A%20Song.wav"
        self.assertEqual(classify(query, url="file:///C:/Music/A%2520Song.wav"), "exact")
        self.assertEqual(classify(query, url="file:///C:/Music/A%20Song.wav"), "fallback_unrelated")

    def test_parent_segments_do_not_claim_symlink_equivalence(self):
        self.assertNotEqual(local_file_identity(r"C:\Music\link\..\A.wav"), local_file_identity(r"C:\Music\A.wav"))
        self.assertNotEqual(local_file_identity("/music/link/../A.wav"), local_file_identity("/music/A.wav"))

    def test_different_file_rejected_despite_same_title_or_basename(self):
        for actual in (OTHER, PATH.replace("qa1482-media-20261007", "another-folder")):
            with self.subTest(actual=actual):
                self.assertEqual(classify(PATH, url=actual, title=TITLE, artist=TITLE), "fallback_unrelated")
        self.assertEqual(classify(PATH, file_path=OTHER, title=TITLE), "fallback_unrelated")

    def test_local_request_cannot_match_remote_stream_with_echoed_title(self):
        for actual in ("https://example.test/track.wav", "spotify:track:synthetic"):
            with self.subTest(actual=actual):
                self.assertEqual(classify(PATH, url=actual, title=PATH), "fallback_unrelated")

    def test_missing_identity_and_query_echo_are_unknown(self):
        for track in ({}, {"title": TITLE}, {"title": PATH, "title_unverified": True}, {"url": "17", "title": TITLE}):
            with self.subTest(track=track):
                self.assertEqual(classify(PATH, **track), "unknown")

    def test_prose_matching_and_unverified_title_behavior_are_unchanged(self):
        for query, track, expected in (
            ("requested song", {"title": "Requested Song"}, "exact"),
            ("requested song", {"title": "Requested Live Version"}, "partial"),
            ("requested song", {"title": "Entirely Different"}, "fallback_unrelated"),
            ("requested song", {"title": "requested song", "title_unverified": True}, "unknown"),
            ("", {"title": TITLE}, "unknown"),
        ):
            with self.subTest(query=query, track=track):
                self.assertEqual(classify(query, **track), expected)
        self.assertIsNone(local_file_identity("qa1482-independent-tone-a-45s.wav"))
        self.assertIsNone(local_file_identity("https://example.test/track.wav"))


class LocalMediaHandlerRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.live = {"probe_ok": True, "is_playing": True, "queue_size": 1, "now_playing": self.track(PATH)}
        self.stop_ok = True
        self.calls = []

        async def post(path, payload=None, **kwargs):
            self.calls.append((path, payload))
            if path == "/v1/play":
                return {"ok": True, "_status_code": 200, "data": {"enqueued": self.track(PATH)}}
            if path == "/v1/stop":
                return {"ok": self.stop_ok, "error": "synthetic output busy" if not self.stop_ok else None}
            raise AssertionError("Unexpected runtime route: " + path)

        self.stack.enter_context(patch.object(music_tools, "_post_runtime", side_effect=post))
        self.stack.enter_context(patch.object(music_tools, "_read_live_playback_state", AsyncMock(side_effect=lambda: self.live)))
        self.stack.enter_context(patch.object(music_tools, "_require_local_runtime_playback_user", Mock()))
        self.stack.enter_context(patch.object(music_tools, "_resolve_required_user_id", return_value="synthetic-user"))
        self.stack.enter_context(patch.object(music_tools, "_active_music_provider_id", return_value="local"))
        self.stack.enter_context(patch.object(music_tools, "_select_provider", Mock()))
        self.stack.enter_context(patch.object(music_tools, "_annotate_requested_provider_fallback", AsyncMock()))
        self.stack.enter_context(patch.object(music_tools, "_annotate_saved_provider_fallback", AsyncMock()))

    @staticmethod
    def track(path):
        return {"title": TITLE, "artist": "Unknown Artist", "url": path, "provider": "local"}

    async def play(self):
        return await music_tools.play_music_handler(PATH, provider="local", user_id="synthetic-user")

    async def test_same_reported_file_is_not_stopped_for_formatted_title(self):
        result = await self.play()
        self.assertTrue(result.ok)
        self.assertFalse(result.unverified)
        self.assertEqual(result.data["query_match"], "exact")
        self.assertEqual([call[0] for call in self.calls], ["/v1/play"])

    async def test_different_live_file_is_not_hidden_by_matching_enqueue_path(self):
        self.live["now_playing"] = self.track(OTHER)
        result = await self.play()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "fallback_unrelated")
        self.assertEqual(result.data["candidate"]["url"], OTHER)
        self.assertTrue(result.data["candidate_stopped"])
        self.assertEqual([call[0] for call in self.calls], ["/v1/play", "/v1/stop"])

    async def test_missing_live_address_does_not_borrow_enqueue_identity(self):
        self.live["now_playing"] = {"title": TITLE, "provider": "local"}
        result = await self.play()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["query_match"], "unknown")
        self.assertEqual([call[0] for call in self.calls], ["/v1/play"])

    async def test_reported_file_path_also_preserves_different_live_identity(self):
        self.live["now_playing"] = {"title": TITLE, "file_path": OTHER, "provider": "local"}
        result = await self.play()
        self.assertFalse(result.ok)
        self.assertEqual(result.data["candidate"]["file_path"], OTHER)
        self.assertTrue(result.data["candidate_stopped"])

    async def test_failed_stop_stays_truthful_and_next_valid_request_recovers(self):
        self.live["now_playing"] = self.track(OTHER)
        self.stop_ok = False
        failure = await self.play()
        self.assertFalse(failure.ok)
        self.assertEqual(failure.data["playback_status"], "candidate_stop_failed")
        self.assertFalse(failure.data["mismatch"]["candidate_not_played"])
        self.assertEqual(failure.data["stop_error"], "synthetic output busy")
        self.live["now_playing"] = self.track(PATH)
        recovered = await self.play()
        self.assertTrue(recovered.ok)
        self.assertEqual(recovered.data["query_match"], "exact")
        self.assertEqual([call[0] for call in self.calls], ["/v1/play", "/v1/stop", "/v1/play"])

    async def test_latest_live_identity_supersedes_existing_candidate(self):
        self.live["now_playing"] = self.track(OTHER)
        result = await music_tools._prefer_resolved_now_playing(
            {"query": PATH, "title": TITLE, "track_uri": PATH, "now_playing": self.track(PATH)}
        )
        rejection = await music_tools._reject_unrelated_candidate_if_needed(PATH, result)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.data["candidate"]["url"], OTHER)

    async def test_matching_identity_does_not_claim_missing_playback_or_probe(self):
        for state in (
            {"probe_ok": True, "is_playing": False, "now_playing": self.track(PATH)},
            {"probe_ok": False, "probe_error": "synthetic unavailable"},
        ):
            with self.subTest(state=state):
                self.live = state
                result = await self.play()
                self.assertTrue(result.ok)
                self.assertTrue(result.unverified)
                self.assertFalse(result.data["playback_verified"])
                self.assertFalse(media_tools._playback_started_evidence(result.ok, result.data, unverified=result.unverified))
        self.assertNotIn("/v1/stop", [call[0] for call in self.calls])

    async def test_normal_title_selection_resolves_id_then_matches_file(self):
        repo = SimpleNamespace(initialize=Mock(), get_track_by_id=Mock(return_value={"file_path": PATH}))
        selection = ToolResult(ok=True, data={"tracks": [{"title": TITLE, "track_uri": "17", "provider": "local"}]})
        with (
            patch.object(media_tools, "_handle_search_mode", AsyncMock(return_value=selection)),
            patch("music.providers.local.db.get_local_library_repo", return_value=repo),
        ):
            result = await media_tools._handle_search_play_mode(TITLE, "local", 10, "", "synthetic-user")
        repo.get_track_by_id.assert_called_once_with(17)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["playback_started"])
        self.assertEqual(result.data["play_result"]["query_match"], "exact")
        self.assertEqual(self.calls, [("/v1/play", {"query": PATH, "source": "local"})])


if __name__ == "__main__":
    unittest.main()
