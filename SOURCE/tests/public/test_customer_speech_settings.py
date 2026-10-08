"""Real settings API/persistence with a synthetic runtime boundary, no inference."""

from __future__ import annotations

import builtins
import os
import types
import unittest
from unittest.mock import Mock, patch

from tests.public import test_speech_volume_wiring as gain_fixture
from tests.public.test_speech_volume_wiring import _isolated_modules


class CustomerSpeechSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gain_fixture.SpeechVolumeWiring.setUpClass()

    def setUp(self):
        self.fixture = gain_fixture.SpeechVolumeWiring(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.patches.enter_context(patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}))
        self.profile = {"selected_voice_ids": ["af_heart", "ef_dora", "zf_xiaobei"], "locales": ["en-us", "es", "zh"]}
        from voice import customer_runtime as runtime
        self.fixture.patches.enter_context(
            patch.object(runtime, "qualification_profile", side_effect=lambda: self.profile)
        )
        self.engine = types.SimpleNamespace(set_customer_route=Mock(), invalidate_customer_route=Mock())
        factory = types.ModuleType("voice.synthesis.factory")
        factory._shared_kokoro = self.engine
        self.factory = factory
        self.fixture.patches.enter_context(_isolated_modules({factory.__name__: factory}))
        import voice.synthesis
        self.fixture.patches.enter_context(patch.object(voice.synthesis, "factory", factory, create=True))
        from config.settings import settings
        self.config = settings
        self.fixture.patches.enter_context(patch.object(settings, "tts_language", "en-us", create=True))
        self.fixture.patches.enter_context(patch.object(settings, "tts_voice", "default"))
        self.fixture.patches.enter_context(patch.object(settings, "_customer_speech_selection_failed", False, create=True))

    def save(self, values, user=None):
        return self.fixture.post_values(values, user=user)

    def test_ordinary_cloud_settings_do_not_import_absent_voice_package(self):
        original_import = builtins.__import__

        def no_voice(name, *args, **kwargs):
            if name == "voice" or name.startswith("voice."):
                raise AssertionError("ordinary settings imported optional voice source")
            return original_import(name, *args, **kwargs)

        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "espeak"}), patch.object(builtins, "__import__", no_voice):
            response = self.read(user="volume-user-a")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("customer_speech", response.json()["data"])
            response = self.save({"tts_voice": "alloy"}, user="volume-user-a")
            self.assertEqual(response.status_code, 200, response.text)

    def read(self, user=None):
        import asyncio
        import httpx

        async def fetch():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.fixture.app), base_url="http://speech.test"
            ) as client:
                return await client.get("/v1/settings", headers={"x-fixture-user": user} if user else {})

        return asyncio.run(fetch())

    def test_authenticated_settings_refresh_switches_existing_engine_with_user(self):
        self.save({"tts_language": "es", "tts_voice": "ef_dora"}, user="volume-user-a")
        self.engine.set_customer_route.reset_mock()
        other = self.read(user="volume-user-b")
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["data"]["customer_speech"]["selection"], {"language": "en-us", "voice": "af_heart"})
        self.engine.set_customer_route.assert_called_once_with("en-us", "af_heart")
        self.read(user="volume-user-b")
        self.engine.set_customer_route.assert_called_once()
        restored = self.read(user="volume-user-a")
        self.assertEqual(restored.json()["data"]["customer_speech"]["selection"], {"language": "es", "voice": "ef_dora"})
        self.engine.set_customer_route.assert_called_with("es", "ef_dora")
        self.assertEqual(self.fixture.manager.get("tts_voice", "default", user_id="volume-user-b"), "default")

    def test_same_user_refresh_does_not_retry_or_clear_a_failed_route(self):
        self.engine.set_customer_route.side_effect = ValueError("synthetic route failure")
        self.save({"tts_language": "es", "tts_voice": "ef_dora"}, user="volume-user-a")
        self.engine.set_customer_route.side_effect = None
        response = self.read(user="volume-user-a")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.engine.set_customer_route.assert_called_once()
        self.assertTrue(self.config._customer_speech_selection_failed)

    def test_account_preference_load_failure_invalidates_previous_route_and_blocks_first_use(self):
        self.save({"tts_language": "es", "tts_voice": "ef_dora"}, user="volume-user-a")
        original_get = self.fixture.manager.get

        def failing_get(key, default=None, user_id=None, **kwargs):
            if key in {"tts_language", "tts_voice"} and kwargs.get("on_load_error") == "raise":
                raise RuntimeError("synthetic account preference read failure")
            return original_get(key, default, user_id=user_id, **kwargs)

        with patch.object(self.fixture.manager, "get", side_effect=failing_get):
            response = self.read(user="volume-user-b")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.engine.invalidate_customer_route.assert_called_once()
        self.assertTrue(self.config._customer_speech_selection_failed)
        self.read(user="volume-user-b")
        self.assertTrue(self.config._customer_speech_selection_failed)
        self.engine.set_customer_route.assert_called_once()
        response = self.save({"tts_language": "en-us", "tts_voice": "af_heart"}, user="volume-user-b")
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "applied")
        self.assertFalse(self.config._customer_speech_selection_failed)

        self.factory._shared_kokoro = None
        with patch.object(self.fixture.manager, "get", side_effect=failing_get):
            response = self.read(user="volume-user-a")
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.assertTrue(self.config._customer_speech_selection_failed)

    def test_each_language_reaches_shared_engine_and_survives_json_reload(self):
        for locale, voice in (("en-us", "af_heart"), ("es", "ef_dora"), ("zh", "zf_xiaobei")):
            response = self.save({"tts_language": locale, "tts_voice": voice})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()["data"]
            self.assertEqual(data["customer_speech"]["selection"], {"language": locale, "voice": voice})
            self.assertEqual(data["customer_speech_effect"]["outcome"], "applied")
            self.engine.set_customer_route.assert_called_with(locale, voice)
            reopened = self.fixture.new_manager()
            self.assertEqual((reopened.get("tts_language"), reopened.get("tts_voice")), (locale, voice))
            self.assertEqual((self.config.tts_language, self.config.tts_voice), (locale, voice))

    def test_pair_persists_for_the_explicit_user_without_changing_other_user(self):
        self.fixture.manager.update({"tts_language": "es", "tts_voice": "ef_dora"}, user_id="volume-user-a")
        response = self.save({"tts_language": "zh", "tts_voice": "zf_xiaobei"}, user="volume-user-b")
        self.assertEqual(response.status_code, 200, response.text)
        self.engine.set_customer_route.assert_called_once_with("zh", "zf_xiaobei")
        for user, expected in (("volume-user-a", ("es", "ef_dora")), ("volume-user-b", ("zh", "zf_xiaobei"))):
            reopened = self.fixture.new_manager()
            self.assertEqual(tuple(reopened.get(key, user_id=user) for key in ("tts_language", "tts_voice")), expected)

    def test_invalid_pair_or_dormant_voice_refuses_the_whole_write(self):
        for values in (
            {"tts_language": "es", "tts_voice": "af_heart"},
            {"tts_language": "fr", "tts_voice": "ff_siwis"},
            {"tts_language": "auto", "tts_voice": "af_heart"},
        ):
            response = self.save({**values, "theme": "light"})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["data"]["validation_reason"], "speech_selection")
            self.assertEqual(self.fixture.manager.get("tts_voice"), "default")
            self.assertNotEqual(self.fixture.manager.get("theme"), "light")
        self.engine.set_customer_route.assert_not_called()

    def test_plain_qa_keeps_existing_voice_behavior_and_refuses_output_locale(self):
        self.profile = None
        response = self.save({"tts_voice": "nova"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("customer_speech", response.json()["data"])
        self.assertEqual(self.fixture.manager.get("tts_voice"), "nova")
        self.assertEqual(self.save({"tts_language": "es"}).status_code, 422)
        self.engine.set_customer_route.assert_not_called()
        self.assertEqual(self.config.tts_voice, "default")

    def test_plain_qa_full_snapshot_can_echo_its_unchanged_dormant_locale(self):
        self.profile = None
        self.assertTrue(self.fixture.manager.update({"tts_language": "en-us", "tts_voice": "nova"}))
        self.assertEqual(self.fixture.new_manager().get("tts_voice"), "nova")
        with self.assertRaises(ValueError):
            self.fixture.manager.update({"tts_language": "es", "tts_voice": "echo"})
        self.assertEqual(self.fixture.manager.get("tts_voice"), "nova")
        self.engine.set_customer_route.assert_not_called()

    def test_stt_and_answer_locale_remain_independent(self):
        response = self.save({"whisper_language": "es", "locale": "zh-CN"})
        self.assertEqual(response.status_code, 200, response.text)
        self.engine.set_customer_route.assert_not_called()
        self.assertEqual(self.fixture.manager.get("tts_voice"), "default")

    def test_first_use_metadata_lists_only_selected_voices_without_materializing(self):
        data = self.fixture.api._settings_response_payload(dict(self.fixture.manager.settings))
        self.assertEqual(data["customer_speech"]["selection"], {"language": "en-us", "voice": "af_heart"})
        self.assertEqual([route["value"] for route in data["customer_speech"]["locales"]], ["en-us", "es", "zh"])
        self.assertFalse(data["customer_speech"]["release_eligible"])
        self.engine.set_customer_route.assert_not_called()

    def test_unmaterialized_engine_receives_selection_through_config(self):
        self.factory._shared_kokoro = None
        response = self.save({"tts_language": "es", "tts_voice": "ef_dora"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "deferred")
        self.assertEqual((self.config.tts_language, self.config.tts_voice), ("es", "ef_dora"))
        self.engine.set_customer_route.assert_not_called()

    def test_refresh_reports_a_later_construction_failure_without_retry(self):
        self.factory._shared_kokoro = None
        response = self.read(user="volume-user-a")
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "deferred")
        self.config._customer_speech_selection_failed = True
        response = self.read(user="volume-user-a")
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.assertTrue(self.config._customer_speech_selection_failed)
        self.engine.set_customer_route.assert_not_called()

    def test_failed_runtime_application_is_reported_as_saved_but_failed(self):
        self.engine.set_customer_route.side_effect = ValueError("synthetic unavailable asset")
        response = self.save({"tts_language": "es", "tts_voice": "ef_dora"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.assertEqual(self.fixture.manager.get("tts_voice"), "ef_dora")
        self.assertEqual((self.config.tts_language, self.config.tts_voice), ("en-us", "default"))

    def test_unchanged_selection_retries_a_previously_failed_application(self):
        self.engine.set_customer_route.side_effect = ValueError("synthetic unavailable asset")
        values = {"tts_language": "es", "tts_voice": "ef_dora"}
        self.assertEqual(self.save(values).json()["data"]["customer_speech_effect"]["outcome"], "failed")
        self.engine.set_customer_route.side_effect = None
        self.assertEqual(self.save(values).json()["data"]["customer_speech_effect"]["outcome"], "applied")
        self.assertEqual(self.engine.set_customer_route.call_count, 2)

    def test_direct_settings_write_validates_and_persists_a_complete_pair(self):
        self.fixture.manager.set("tts_language", "es")
        reopened = self.fixture.new_manager()
        self.assertEqual((reopened.get("tts_language"), reopened.get("tts_voice")), ("es", "ef_dora"))
        with self.assertRaises(ValueError):
            self.fixture.manager.update({"tts_language": "zh", "tts_voice": "ef_dora"})
        self.assertEqual(self.fixture.manager.get("tts_language"), "es")

    def test_reset_reapplies_default_output_route(self):
        import asyncio
        import httpx

        self.save({"tts_language": "es", "tts_voice": "ef_dora"})
        async def reset():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.fixture.app), base_url="http://speech.test"
            ) as client:
                return await client.post("/v1/settings/reset")
        response = asyncio.run(reset())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["customer_speech_effect"]["outcome"], "applied")
        self.engine.set_customer_route.assert_called_with("en-us", "af_heart")

    def test_failed_persistence_does_not_apply(self):
        with patch.object(self.fixture.manager, "save", return_value=False):
            response = self.save({"tts_language": "es", "tts_voice": "ef_dora"})
        self.assertEqual(response.status_code, 500, response.text)
        self.engine.set_customer_route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
