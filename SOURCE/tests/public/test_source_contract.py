"""Checks that can be run directly inside a source distribution."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SourceContract(unittest.TestCase):
    def test_chat_catalog_migrates_only_managed_model_labels(self):
        import sys
        import types
        from types import SimpleNamespace
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT))
        source = ROOT / 'ui/api/routes/chat_mode.py'
        tree = ast.parse(source.read_text())
        names = {'_unique_models', '_provider_prefix_valid', '_chat_model_catalog', '_validate_chat_model'}
        nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
        nodes += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), 'exec'), namespace)
        base = types.ModuleType('services.llm.providers.base')
        base.PROVIDER_INFO = {'openai': SimpleNamespace(name='OpenAI', popular_models=['gpt-5.4-mini'], default_models=['gpt-5.4-mini'])}
        settings = types.ModuleType('ui.settings_manager')
        for ai_source, expected in [('managed', 'gpt-6-luna'), ('subscription', 'gpt-6-luna'), ('byok', 'gpt-5.4-mini')]:
            def get(key, default=None, *, user_id):
                self.assertEqual(user_id, 'synthetic-user')
                return {'ai_source': ai_source, 'llm_provider': 'openai', 'llm_model': 'gpt-5.4-mini'}.get(key, default)
            settings.get_settings_manager = lambda: SimpleNamespace(get=get)
            with patch.dict(sys.modules, {'services.llm.providers.base': base, 'ui.settings_manager': settings}):
                catalog = namespace['_chat_model_catalog']('synthetic-user')
                self.assertEqual(catalog['current_model'], expected)
                self.assertEqual(namespace['_validate_chat_model']('synthetic-user', 'gpt-5.4-mini', explicit=True), expected)
                if ai_source != 'byok': self.assertNotIn('gpt-5.4-mini', catalog['models'])


    def test_managed_luna_default_preserves_other_sources(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from config import defaults
        for source in ('managed', 'subscription'):
            for agent in (False, True):
                for candidates in ((), ('',), ('gpt-5.4-mini',), ('  gpt-5.4-mini  ',)):
                    with self.subTest(source=source, agent=agent, candidates=candidates):
                        self.assertEqual(defaults.resolve_effective_model(ai_source=source, agent=agent, candidates=candidates), 'gpt-6-luna')
        self.assertEqual((defaults.DEFAULT_GPT_MODEL, defaults.DEFAULT_AGENT_MODEL, defaults.DEFAULT_PHONE_MODEL), ('gpt-5.4-mini',) * 3)
        self.assertEqual(defaults.get_provider_default_model('openai'), 'gpt-5.4-mini')
        self.assertEqual(defaults.get_provider_default_agent_model('openai'), 'gpt-5.4-mini')
        self.assertEqual(defaults.resolve_effective_model(ai_source='byok', candidates=('gpt-5.4-mini',)), 'gpt-5.4-mini')
        self.assertEqual(defaults.resolve_effective_model(ai_source='local', provider='ollama', candidates=('fixture:latest',)), 'fixture:latest')
        self.assertEqual(defaults.resolve_effective_model(ai_source='codex'), 'gpt-5.4-mini')
        self.assertEqual(defaults.resolve_effective_model(ai_source='managed', candidates=('gpt-4o-mini',)), 'gpt-4o-mini')


    def test_luna_reasoning_parameters_preserve_phone_tier(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from config import defaults
        self.assertEqual(defaults.resolve_reasoning_effort('none', 'gpt-6-luna'), 'none')
        self.assertEqual(defaults.resolve_reasoning_effort('minimal', 'gpt-6-luna'), 'none')
        self.assertTrue(defaults._is_reasoning_family('gpt-6-luna'))
        self.assertEqual(defaults.pipecat_model_extra('gpt-6-luna', 'low'), {'reasoning': {'effort': 'low', 'summary': 'auto'}})
        self.assertEqual(defaults.pipecat_phone_model_extra('gpt-6-luna', 'low', tools_present=False), {'reasoning_effort': 'low'})
        # Legacy Chat Completions only accepts Luna tool calls with none;
        # production Responses phone turns retain their low effort above.
        self.assertEqual(defaults.pipecat_phone_model_extra('gpt-6-luna', 'low', tools_present=True), {'reasoning_effort': 'none'})


    def test_luna_pricing_includes_long_context_threshold(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from services.llm import pricing
        pricing.reset_unknown_model_cost_state()
        self.assertEqual(pricing.get_llm_pricing_usd('gpt-6-luna')['input'], 0.10)
        self.assertAlmostEqual(pricing.calculate_cost_cents('gpt-6-luna', 200000, 100000, 100000, cache_write_tokens=10000), 6.225)
        self.assertAlmostEqual(pricing.calculate_cost_cents('gpt-6-luna', 272000, 100000), 7.72)
        self.assertAlmostEqual(pricing.calculate_cost_cents('gpt-6-luna', 272001, 100000), 12.94002)
        self.assertFalse(pricing.has_unknown_model_cost())


    def test_phone_cost_uses_carrier_facts_or_measured_estimate_without_outcome_floor(self):
        """Exercise the actual source function without importing optional phone runtimes."""
        from types import SimpleNamespace

        source = ROOT / "telephony" / "call_manager.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        constants = {"_MIN_BILLED_COST_USD", "_BILLABLE_FAILURE_STATES"}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_billed_cost_usd":
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in constants for target in node.targets
            ):
                nodes.append(node)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in constants:
                nodes.append(node)
        namespace = {}
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        billed_cost = namespace["_billed_cost_usd"]

        def record(status, estimate, carrier):
            return SimpleNamespace(
                status=SimpleNamespace(value=status),
                estimated_cost_usd=estimate,
                carrier_total_cost_usd=carrier,
            )

        # Existing shared source turned this measured estimate into an unsupported cent.
        self.assertEqual(billed_cost(record("no_answer", 0.004, None)), 0.004)
        for status in ("failed", "timeout", "no_answer", "cancelled", "completed", "voicemail"):
            for estimate in (-0.004, 0.0, 0.004, 0.25, None):
                for carrier in (None, -0.004, 0.0, 0.004, 0.42):
                    with self.subTest(status=status, estimate=estimate, carrier=carrier):
                        expected = max(0.0, float(carrier if carrier is not None else estimate or 0.0))
                        self.assertEqual(billed_cost(record(status, estimate, carrier)), expected)

    def test_audio_ducking_never_amplifies_quiet_playback(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from utils.audio_ducking import AudioDucker

        class State:
            volume = 5
            is_playing = True

        class Player:
            volume = 5

            @staticmethod
            def state():
                return State()

            def set_volume(self, level):
                self.volume = level

        player = Player()
        ducker = AudioDucker(player, duck_level=20, fade_duration=0.1)
        ducker.duck()
        ducker._fade_thread.join(timeout=1)
        self.assertEqual(player.volume, 5)
        ducker.unduck()
        ducker._fade_thread.join(timeout=1)
        self.assertEqual(player.volume, 5)

    def test_music_player_legacy_volume_matches_restored_state(self):
        import logging
        import sys

        sys.path.insert(0, str(ROOT))
        from music.player.initializer import MusicPlayerInitializer

        class Store:
            @staticmethod
            def load_music_state(_user_id):
                return {"queue": [], "now_playing": None, "volume": 5, "is_playing": False}

            @staticmethod
            def clear_stale_queue(_user_id):
                return 0

        class Player:
            _logger = logging.getLogger("test.music.restore")
            _test_mode = False
            _volume = 80

        player = Player()
        MusicPlayerInitializer(player)._setup_core_services(None, Store())
        self.assertEqual(player._state.volume, 5)
        self.assertEqual(player._volume, 5)

    def test_lazy_music_placeholder_cannot_seed_hub_defaults(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from ui.core.player_state import to_player_state

        class LazyMusic:
            @staticmethod
            def state():
                return {}

            @staticmethod
            def is_materialized():
                return False

        class RecordingHub:
            calls = 0

            def reconcile_provider_state(self, **_kwargs):
                self.calls += 1
                raise AssertionError("lazy placeholder must not reach hub reconciliation")

        hub = RecordingHub()
        state = to_player_state(LazyMusic(), object(), hub_authority=hub)
        self.assertEqual(hub.calls, 0)
        self.assertFalse(state.is_playing)

    def test_music_state_startup_uses_desktop_device_partition(self):
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(ROOT))
        from music.runtime.state_service import PlayerStateService

        with (
            patch("core.user_context.get_current_user_id", side_effect=LookupError),
            patch("core.user_context.get_device_user_id", return_value="device-public-source"),
        ):
            self.assertEqual(PlayerStateService._resolve_user_id(), "device-public-source")

    def test_cloud_phone_history_fails_cleanly_without_private_proxy(self):
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(ROOT))
        from telephony.routes import _maybe_proxy_phone_to_cloud

        with patch("telephony.phone_mode.phone_mode_is_cloud", return_value=True):
            response = asyncio.run(_maybe_proxy_phone_to_cloud("GET", "/api/phone/history"))

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "cloud_phone_unavailable")

    def test_account_gate_is_shipped_and_keeps_user_owned_ai_account_free(self):
        import os
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(ROOT))
        from core.account_gate import requires_account_for_command

        with patch.dict(os.environ, {"VIOLA_REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_OVERRIDE": "true"}):
            self.assertTrue(requires_account_for_command("device-public-source", "managed"))
            self.assertFalse(requires_account_for_command("device-public-source", "byok"))
            self.assertFalse(requires_account_for_command("device-public-source", "codex"))
            self.assertFalse(requires_account_for_command("device-public-source", "local"))

    def test_managed_budget_honors_global_source_override_before_private_billing(self):
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(ROOT))
        from config.settings import settings as app_settings
        from services.llm.managed_budget import user_uses_managed_llm

        with patch.object(app_settings, "ai_source_override", "local"):
            self.assertFalse(user_uses_managed_llm("device-public-source"))
        with patch.object(app_settings, "ai_source_override", "byok"):
            self.assertFalse(user_uses_managed_llm("device-public-source"))
        with patch.object(app_settings, "ai_source_override", "managed"):
            self.assertTrue(user_uses_managed_llm("device-public-source"))

    def test_desktop_security_guard_imports_are_shipped(self):
        for folder in ("mcp_hub", "mcp_servers", "services/computer_use"):
            for file in (ROOT / folder).rglob("*.py"):
                tree = ast.parse(file.read_text(encoding="utf-8-sig"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("services.computer_use."):
                        module = ROOT.joinpath(*node.module.split("."))
                        assert (
                            module.with_suffix(".py").is_file() or (module / "__init__.py").is_file()
                        ), f"{file.relative_to(ROOT)} requires {node.module}"

    @staticmethod
    def _aiohttp_minimum(requirements_text: str) -> tuple[int, int, int]:
        entries = [line.split("#", 1)[0].strip() for line in requirements_text.splitlines()]
        aiohttp = [line for line in entries if line.lower().startswith("aiohttp")]
        if len(aiohttp) != 1:
            raise ValueError("expected exactly one aiohttp requirement")
        match = re.match(r"^aiohttp\s*>=\s*(\d+)\.(\d+)\.(\d+)", aiohttp[0], re.IGNORECASE)
        if match is None:
            raise ValueError("aiohttp requirement must declare a minimum version")
        return tuple(int(part) for part in match.groups())

    @classmethod
    def _aiohttp_floor_is_fixed(cls, requirements_text: str) -> bool:
        return cls._aiohttp_minimum(requirements_text) >= (3, 14, 3)

    def test_aiohttp_security_floor_excludes_cve_2026_69244(self):
        self.assertFalse(self._aiohttp_floor_is_fixed("aiohttp >= 3.14.2, <4.0.0  # affected"))
        self.assertTrue(self._aiohttp_floor_is_fixed("aiohttp>=3.15.0,<4.0.0 # a future patched floor"))
        for requirements in (
            "requirements_desktop.txt",
            "requirements_linux.txt",
            "requirements_macos.txt",
        ):
            with self.subTest(requirements=requirements):
                self.assertTrue(self._aiohttp_floor_is_fixed((ROOT / requirements).read_text(encoding="utf-8")))

    def test_maintained_dependency_source_inventory(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from scripts.dependencies.verify_sources import verify_sources

        assert verify_sources(ROOT) == []

    def test_setup_and_notices_are_shipped(self):
        for path in (
            "README.md",
            ".env.example",
            "LICENSE",
            "NOTICES.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/INSTALLATION.md",
            "docs/CONFIGURATION.md",
            "docs/TELEPHONY.md",
            "docs/VERIFYING.md",
            "scripts/download_models.py",
            "ui/react-app/package-lock.json",
            "LICENSES/Silero-MIT.txt",
        ):
            with self.subTest(path=path):
                assert (ROOT / path).is_file(), path

    def test_private_material_is_absent(self):
        for path in (
            "billing",
            "admin",
            "ops",
            "memory",
            "db",
            "_diag",
            "backend/webhooks/gotrue.py",
            "backend/webhooks/standard_webhook.py",
            "ui/api/routes/public_stats.py",
            "ui/api/routes/sync_bulk.py",
            "auth/desktop_gotrue_proxy.py",
            "diagnostics/diagnostic_relay.py",
            "services/payments",
            "telephony/phone_billing.py",
            "telephony/desktop_cloud_proxy.py",
            "updates/manifests",
        ):
            with self.subTest(path=path):
                assert not (ROOT / path).exists(), path

    def test_shipped_asset_rights_match_bytes(self):
        inventory = tomllib.loads((ROOT / "LICENSES/asset_inventory.toml").read_text())
        for item in inventory["assets"]:
            with self.subTest(path=item["path"]):
                assert hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]
                assert (ROOT / item["license_file"]).is_file()
                assert item["origin"]

    def test_rejected_model_dependency_cannot_be_imported_by_runtime_source(self):
        for folder in ("telephony", "third_party/pipecat/src"):
            for file in (ROOT / folder).rglob("*.py"):
                tree = ast.parse(file.read_text(encoding="utf-8-sig"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        assert not any(alias.name.split(".")[0] == "nltk" for alias in node.names), str(file)
                    elif isinstance(node, ast.ImportFrom):
                        assert (node.module or "").split(".")[0] != "nltk", str(file)


class CurrentTrackRatingContract(unittest.IsolatedAsyncioTestCase):
    async def _rate(self, music, value):
        import logging
        import sys
        import types
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        source = ROOT / 'ui/api/routes/rating.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        register = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'register_rating_routes')
        isolated = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), register], type_ignores=[])
        routes = {}
        class Router:
            def post(self, path, **kwargs):
                def save(fn): routes[path] = fn; return fn
                return save
            get = post
            delete = post
        class Toolbox:
            async def record_and_call(self, callback, **kwargs): return await callback()
        rating = SimpleNamespace(thumbs_up=Mock(), thumbs_down=Mock(), remove_rating=Mock())
        module = types.ModuleType('music.rating_system');module.get_rating_system=lambda:rating
        namespace = {'log':logging.getLogger('rating-contract'),'Body':lambda value:value,
                     'Depends':lambda value:value,'require_auth':lambda:None,
                     'error_response':lambda message,status_code=400:{'ok':False,'status':status_code,'error':message},
                     'handle_route_error':lambda exc,route:{'ok':False,'status':500,'error':str(exc)}}
        exec(compile(ast.fix_missing_locations(isolated),str(source),'exec'),namespace)
        context=SimpleNamespace(router=Router(),bindings=SimpleNamespace(music=music))
        namespace['register_rating_routes'](context,Toolbox())
        with patch.dict(sys.modules,{'music.rating_system':module}):
            result=await routes['/v1/rating']({'rating':value})
        return result,rating

    async def test_current_local_track_can_be_liked_disliked_and_cleared(self):
        from types import SimpleNamespace
        music=SimpleNamespace(current_track=SimpleNamespace(id='local-synthetic',video_id=None,title='Acceptance tone',artist='Synthetic'))
        for value,method in [('liked','thumbs_up'),('disliked','thumbs_down'),(None,'remove_rating')]:
            with self.subTest(value=value):
                result,rating=await self._rate(music,value)
                self.assertEqual(result,{'ok':True,'video_id':'local-synthetic','rating':value})
                if value is None: rating.remove_rating.assert_called_once_with('local-synthetic')
                else: getattr(rating,method).assert_called_once_with(video_id='local-synthetic',title='Acceptance tone',artist='Synthetic')

    async def test_object_and_dictionary_player_states_resolve_the_same_track(self):
        from types import SimpleNamespace
        track={'id':'synthetic-current','title':'Current'}
        for state in [{'now_playing':track},SimpleNamespace(now_playing=track)]:
            result,rating=await self._rate(SimpleNamespace(get_state=lambda:state),'liked')
            self.assertTrue(result['ok'],result)
            rating.thumbs_up.assert_called_once_with(video_id='synthetic-current',title='Current',artist=None)

    async def test_missing_track_and_invalid_rating_do_not_mutate_ratings(self):
        from types import SimpleNamespace
        for music,value in [(None,'liked'),(SimpleNamespace(current_track=None),'liked'),(SimpleNamespace(current_track={'id':'synthetic'}),'invalid')]:
            result,rating=await self._rate(music,value)
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'],400)
            rating.thumbs_up.assert_not_called();rating.thumbs_down.assert_not_called();rating.remove_rating.assert_not_called()


class OnboardingSavedStepContract(unittest.TestCase):
    def test_all_frontend_saved_step_identifiers_are_accepted_by_backend_enum(self):
        from enum import Enum
        source = ROOT / 'ui/onboarding.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        enum_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OnboardingStep')
        namespace = {'Enum': Enum}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[enum_node], type_ignores=[])), str(source), 'exec'), namespace)
        frontend = (ROOT / 'ui/react-app/src/hooks/useVoiceOnboarding.js').read_text(encoding='utf-8')
        identifiers = set(re.findall(r"saveOnboardingStep\('([^']+)'", frontend))
        self.assertIn('autonomy_tier', identifiers)
        for identifier in identifiers:
            self.assertEqual(namespace['OnboardingStep'](identifier).value, identifier)


if __name__ == "__main__":
    unittest.main()


class RepeatPlaybackContract(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, mode, *, reject_preference=False):
        import logging
        import sys
        import types
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        sys.path.insert(0, str(ROOT))
        from models.state_manager import ConsolidatedState, RepeatMode
        from music.playback_session import PlaybackSessionController
        controller = PlaybackSessionController(state_manager=ConsolidatedState())
        preferences = {'mode': 'off', 'user_id': None}
        class Compat:
            def __init__(self, *, user_id=None): preferences['user_id'] = user_id
            def get_repeat_mode(self): return preferences['mode']
            def set_repeat_mode(self, value):
                if reject_preference: raise RuntimeError('synthetic preference rejection')
                preferences['mode'] = value
        compat = types.ModuleType('core.compat'); compat.StateCompat = Compat
        playback = types.ModuleType('music.playback_session')
        playback.get_playback_session_controller = lambda: controller
        helpers = types.ModuleType('utils.api_helpers'); helpers.inject_preferences = lambda value: None
        source = ROOT / 'ui/api/routes/control.py'
        tree = ast.parse(source.read_text())
        endpoint = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'post_repeat')
        endpoint.decorator_list = []
        endpoint.args.defaults = [ast.Constant(None)]
        async def record(callback, **kwargs): return await callback()
        async def snapshot(*args): return SimpleNamespace(model_dump=lambda: {})
        namespace = {'log': logging.getLogger('repeat-contract'), 'toolbox': SimpleNamespace(record_and_call=record),
                     'music': object(), 'state': {}, 'hub': SimpleNamespace(broadcast=AsyncMock()),
                     '_safe_state_adapter': snapshot,
                     '_control_error_response': lambda status, code, message: {'ok': False, 'status': status, 'error': code}}
        isolated = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), endpoint], type_ignores=[])
        exec(compile(ast.fix_missing_locations(isolated), str(source), 'exec'), namespace)
        with patch.dict(sys.modules, {'core.compat': compat, 'music.playback_session': playback, 'utils.api_helpers': helpers}):
            result = await namespace['post_repeat'](SimpleNamespace(json=AsyncMock(return_value={'mode': mode})), user_id='synthetic-listener')
        return result, controller, preferences

    async def test_repeat_endpoint_changes_real_playback_and_display_preference(self):
        from models.state_manager import RepeatMode
        for mode in ('off', 'all', 'one'):
            with self.subTest(mode=mode):
                result, controller, preferences = await self._exercise(mode)
                self.assertTrue(result['ok'])
                self.assertEqual(controller.get_repeat_mode(), RepeatMode(mode))
                self.assertEqual(controller.should_repeat_current(), mode == 'one')
                self.assertEqual(controller.should_loop_queue(), mode == 'all')
                self.assertEqual(preferences, {'mode': mode, 'user_id': 'synthetic-listener'})

    async def test_failed_preference_write_does_not_leave_runtime_changed(self):
        from models.state_manager import RepeatMode
        result, controller, preferences = await self._exercise('one', reject_preference=True)
        self.assertFalse(result['ok'])
        self.assertEqual(controller.get_repeat_mode(), RepeatMode.OFF)
        self.assertEqual(preferences['mode'], 'off')

    async def test_invalid_repeat_does_not_change_runtime_or_preference(self):
        from models.state_manager import RepeatMode
        result, controller, preferences = await self._exercise('invalid')
        self.assertEqual(result['status'], 400)
        self.assertEqual(controller.get_repeat_mode(), RepeatMode.OFF)
        self.assertEqual(preferences['mode'], 'off')

    async def test_controller_voice_cycle_keeps_display_preference_in_sync(self):
        import sys
        import types
        from unittest.mock import patch
        from models.state_manager import ConsolidatedState, RepeatMode
        from music.playback_session import PlaybackSessionController
        writes = []
        class Compat:
            def __init__(self, *, user_id=None): self.user_id = user_id
            def set_repeat_mode(self, mode): writes.append((self.user_id, mode))
        compat = types.ModuleType('core.compat'); compat.StateCompat = Compat
        controller = PlaybackSessionController(state_manager=ConsolidatedState())
        with patch.dict(sys.modules, {'core.compat': compat}):
            for expected in (RepeatMode.ALL, RepeatMode.ONE, RepeatMode.OFF):
                self.assertEqual(controller.cycle_repeat_mode(), expected)
                self.assertEqual(writes[-1], (None, expected.value))
            controller.set_repeat_mode(RepeatMode.ONE, user_id='explicit-synthetic-listener')
            self.assertEqual(writes[-1], ('explicit-synthetic-listener', 'one'))
            with self.assertRaises(RuntimeError):
                PlaybackSessionController().set_repeat_mode(RepeatMode.ONE)
            self.assertEqual(len(writes), 4)


class HotkeyCollisionContract(unittest.TestCase):
    def test_keyboard_code_and_display_alias_collisions_are_rejected(self):
        from types import SimpleNamespace
        source = ROOT / 'ui/settings_api.py'
        tree = ast.parse(source.read_text())
        names = {'_normalize_hotkey_for_compare', '_validate_hotkey_cross_field_requirements'}
        nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
        nodes += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), 'exec'), namespace)
        validate = namespace['_validate_hotkey_cross_field_requirements']
        same = [('Ctrl+KeyM', 'ctrl+m'), ('Control+M', 'ctrl+m'), ('Shift+Ctrl+KeyM', 'control+shift+m'),
                ('Alt+Digit1', 'option+1'), ('Super+Spacebar', 'Win+Space'), ('Escape', 'Esc'), ('Return', 'Enter')]
        for ptt, mute in same:
            with self.subTest(ptt=ptt, mute=mute):
                settings = SimpleNamespace(get=lambda key, fallback: {'ptt_hotkey': ptt, 'mute_hotkey': mute}.get(key, fallback))
                self.assertTrue(validate({'ptt_hotkey': ptt}, settings))
        for ptt, mute in [('ctrl+m','m'), ('Digit1','Numpad1'), ('ctrl+m','ctrl+shift+m'), ('Space','ctrl+m')]:
            with self.subTest(ptt=ptt, mute=mute):
                settings = SimpleNamespace(get=lambda key, fallback: {'ptt_hotkey': ptt, 'mute_hotkey': mute}.get(key, fallback))
                self.assertEqual(validate({'ptt_hotkey': ptt}, settings), [])


class MediaPlaybackEvidenceContract(unittest.IsolatedAsyncioTestCase):
    async def test_media_play_preserves_unverified_result_from_real_music_verdict(self):
        from unittest.mock import AsyncMock, patch
        from intent.tools import media_tools, music_tools

        result = music_tools._play_tool_result(
            {"title": "Synthetic", "verification": music_tools.VERIFY_NOT_PLAYING, "playback_verified": False}
        )
        self.assertTrue(result.unverified)
        with patch.object(media_tools, "_play_music_handler", AsyncMock(return_value=result)):
            actual = await media_tools._handle_play_mode("/synthetic/track.wav", "local", "", "fixture-user")
        self.assertTrue(actual.ok)  # Accepted is real, even though playback is unconfirmed.
        self.assertTrue(actual.unverified)
        self.assertFalse(actual.data["playback_verified"])
        self.assertEqual(actual.data["message"], result.data["message"])

    async def test_search_play_cannot_promote_accepted_or_failed_verification_to_started(self):
        from unittest.mock import AsyncMock, patch
        from intent.tool_types import ToolResult
        from intent.tools import media_tools, music_tools

        track = {"title": "Synthetic", "track_uri": "/synthetic/track.wav", "provider": "local"}
        for verification in (
            music_tools.VERIFY_NOT_PLAYING,
            music_tools.VERIFY_UNAVAILABLE,
            music_tools.VERIFY_PLAYING,
        ):
            with self.subTest(verification=verification):
                verified = verification == music_tools.VERIFY_PLAYING
                underlying = music_tools._play_tool_result(
                    {"title": "Synthetic", "verification": verification, "playback_verified": verified}
                )
                with patch.object(
                    media_tools,
                    "_handle_search_mode",
                    AsyncMock(return_value=ToolResult(ok=True, data={"tracks": [track]})),
                ), patch.object(media_tools, "_play_music_handler", AsyncMock(return_value=underlying)):
                    actual = await media_tools._handle_search_play_mode("Synthetic", "local", 5, "", "fixture-user")
                self.assertTrue(actual.ok)
                self.assertEqual(actual.unverified, not verified)
                self.assertEqual(actual.data["playback_started"], verified)
                self.assertEqual(actual.data["play_result"]["playback_verified"], verified)
                if not verified:
                    self.assertNotIn("Playing Synthetic", actual.data["voice_summary"])

    def test_started_requires_positive_evidence_and_honors_explicit_failure(self):
        from intent.tools.media_tools import _playback_started_evidence

        for payload in (
            None,
            {},
            {"enqueued": {}},
            {"playback_verified": False},
            {"room_route": {}},
            {"state": "not_played", "playback_verified": True},
            {"playback_status": "candidate_not_played", "playback_verified": True},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(_playback_started_evidence(True, payload))
        self.assertTrue(_playback_started_evidence(True, {"playback_verified": True}))
        self.assertTrue(_playback_started_evidence(True, {"playback_status": "started"}))
        self.assertFalse(_playback_started_evidence(False, {"playback_verified": True}))
        self.assertFalse(_playback_started_evidence(True, {"playback_verified": True}, unverified=True))


class ProviderValidationEvidenceContract(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_native_probe_cannot_pass_profile_validation_and_valid_retry_recovers(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch

        from services.connectors.profiles import validate_llm_profile
        from services.llm.factory import LLMProviderFactory

        profile = SimpleNamespace(
            category="llm",
            profile_id="fixture-profile",
            connector_id="llm.ollama",
            adapter="ollama",
            model="fixture-model",
            base_url="http://127.0.0.1:11434",
        )
        store = SimpleNamespace(
            get_profile=Mock(return_value=profile),
            get_profile_secret=Mock(return_value=""),
            update_validation=Mock(),
        )
        provider = SimpleNamespace(
            SUPPORTS_NATIVE_TOOLS=True,
            test_connection=AsyncMock(
                return_value=SimpleNamespace(success=True, message="connected", latency_ms=1, error_code=None)
            ),
            route_command_native=AsyncMock(),
            get_available_models=lambda: ["fixture-model"],
        )
        cases = [
            (json.JSONDecodeError("Malformed HTTP200 JSON", "not json", 0), False),
            ("not a protocol object", False),
            ({"type": "tool_call", "tool": "echo_probe", "args": {"value": "wrong"}}, False),
            ({"type": "tool_call", "tool": "echo_probe", "args": {"value": "ok"}}, True),
        ]
        with patch.object(LLMProviderFactory, "create_provider", return_value=provider), patch(
            "socket.socket.connect", side_effect=AssertionError("external network prohibited")
        ):
            for reply, valid in cases:
                with self.subTest(reply=repr(reply)):
                    provider.route_command_native.side_effect = reply if isinstance(reply, Exception) else None
                    provider.route_command_native.return_value = reply
                    result = await validate_llm_profile(store, "fixture-user", "fixture-profile", probe_tools=True)
                    self.assertEqual(result["valid"], valid)
                    self.assertEqual(result["tool_contract"]["live_tool_probe"]["success"], valid)
                    self.assertEqual(result["tool_contract"]["native_tools_supported"], valid)
                    self.assertEqual(result["error_code"], None if valid else "native_tool_contract_failed")
                    store.update_validation.assert_called_with("fixture-user", "fixture-profile", result)
                    store.get_profile.assert_called_with("fixture-user", "fixture-profile")

class ChatStreamRecoveryContract(unittest.IsolatedAsyncioTestCase):
    """Run the actual existing route handlers against synthetic tasks and store.

    Only optional application startup imports/decorators are omitted. No live
    principal, provider, GUI, microphone, network request, or account is used.
    """

    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from http import HTTPStatus
        from fastapi import HTTPException

        self.namespace_type = SimpleNamespace
        self.store = SimpleNamespace(
            get_thread=AsyncMock(return_value={"id": "thread-a"}),
            list_messages=AsyncMock(return_value=[]),
        )
        self.active = {}
        self.request = SimpleNamespace(user_id="owner-a")
        source = ROOT / "ui/api/routes/chat_mode.py"
        tree = ast.parse(source.read_text())
        register = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "register_chat_mode_routes")
        handlers = [node for node in register.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name in {"get_thread", "cancel_stream"}]
        for handler in handlers:
            handler.decorator_list = []

        async def record_and_call(function, **_):
            return await function()

        self.namespace = {
            "_store": AsyncMock(return_value=self.store),
            "_require_request_user_id": lambda request: request.user_id,
            "_thread_payload": lambda record: record,
            "_message_payload": lambda record: record,
            "_ACTIVE_CHAT_TASKS": self.active,
            "_not_found": lambda _: HTTPException(status_code=404),
            "success_response": lambda data: data,
            "failure_response": lambda code, message: {"code": code, "message": message},
            "HTTPException": HTTPException,
            "HTTPStatus": HTTPStatus,
            "toolbox": SimpleNamespace(record_and_call=record_and_call),
        }
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *handlers]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), self.namespace)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def add_task(self, stream_id="stream-a", user="owner-a", thread="thread-a", coroutine=None):
        async def waiting():
            await asyncio.Event().wait()
        task = asyncio.create_task(coroutine or waiting())
        self.tasks.append(task)
        self.active[stream_id] = self.namespace_type(user_id=user, thread_id=thread, task=task)
        return task

    async def get_thread(self):
        return await self.namespace["get_thread"](self.request, "thread-a")

    async def test_liveness_is_owner_and_thread_scoped_and_excludes_finished_tasks(self):
        own = self.add_task()
        self.add_task("foreign", user="owner-b")
        self.add_task("other-thread", thread="thread-b")
        finished = self.add_task("finished")
        finished.cancel()
        await asyncio.gather(finished, return_exceptions=True)
        self.assertEqual((await self.get_thread())["active_stream_ids"], ["stream-a"])
        self.assertFalse(own.done())
        self.store.get_thread.assert_awaited_with("owner-a", "thread-a")
        self.store.list_messages.assert_awaited_with("owner-a", "thread-a", newest=True)

    async def test_task_finishing_during_message_read_cannot_report_false_terminal_snapshot(self):
        task = self.add_task()
        async def stale_read(*_, **__):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return []
        self.store.list_messages.side_effect = stale_read
        first = await self.get_thread()
        self.assertEqual(first["active_stream_ids"], ["stream-a"])
        self.assertEqual((await self.get_thread())["active_stream_ids"], [])

    async def test_cancel_acceptance_is_not_a_terminal_event_until_task_cleanup_finishes(self):
        import sys
        import types
        from unittest.mock import Mock, patch

        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        async def producer():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await allow_cleanup.wait()
                self.store.list_messages.return_value = [{
                    "id": "result", "role": "assistant", "status": "stopped",
                    "content": "Stopped.", "metadata": {"stream_id": "stream-a"},
                }]
                raise
        task = self.add_task(coroutine=producer())
        await asyncio.sleep(0)
        bus = types.ModuleType("services.llm.stream_bus")
        bus.finalize_stream = Mock()
        with patch.dict(sys.modules, {"services.llm.stream_bus": bus}):
            result = await self.namespace["cancel_stream"](self.request, "stream-a")
        self.assertEqual(result, {"cancelled": True})
        bus.finalize_stream.assert_not_called()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        self.assertEqual((await self.get_thread())["active_stream_ids"], ["stream-a"])
        # Repeated Stop cannot inject another cancellation into the pending
        # store write, otherwise both durable status and terminal SSE are lost.
        self.assertEqual(await self.namespace["cancel_stream"](self.request, "stream-a"), {"cancelled": True})
        self.assertEqual(task.cancelling(), 1)
        allow_cleanup.set()
        await asyncio.gather(task, return_exceptions=True)
        terminal = await self.get_thread()
        self.assertEqual(terminal["active_stream_ids"], [])
        self.assertEqual(terminal["messages"][0]["status"], "stopped")

    async def test_cancel_denies_another_owner_and_reports_no_task_without_fabricating_success(self):
        from fastapi import HTTPException
        task = self.add_task(user="owner-b")
        with self.assertRaises(HTTPException) as raised:
            await self.namespace["cancel_stream"](self.request, "stream-a")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(task.cancelling(), 0)
        self.assertEqual(await self.namespace["cancel_stream"](self.request, "absent"), {"cancelled": False})

    async def test_missing_thread_is_rejected_before_liveness_or_messages_are_exposed(self):
        from fastapi import HTTPException
        self.add_task()
        self.store.get_thread.return_value = None
        with self.assertRaises(HTTPException) as raised:
            await self.get_thread()
        self.assertEqual(raised.exception.status_code, 404)
        self.store.list_messages.assert_not_awaited()

    async def test_sqlite_recent_window_recovers_result_after_200_without_losing_history(self):
        import tempfile
        from services.persistence.chat_store import SqliteChatBackend
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            backend = SqliteChatBackend(Path(directory) / "chat.sqlite3")
            await backend.initialize()
            await backend.create_thread("owner-a", thread_id="thread-a", title="Synthetic")
            await backend.create_thread("owner-b", thread_id="thread-a", title="Foreign")
            for index in range(205):
                with patch("services.persistence.chat_store._now", return_value=float(index + 1)):
                    await backend.append_message(
                        "owner-a", "thread-a", role="assistant", content=str(index),
                        metadata={"stream_id": "stream-a"} if index == 204 else {},
                    )
            await backend.append_message("owner-b", "thread-a", role="assistant", content="PRIVATE FOREIGN")
            earliest = await backend.list_messages("owner-a", "thread-a")
            latest = await backend.list_messages("owner-a", "thread-a", newest=True)
            self.assertEqual([message.content for message in earliest], [str(index) for index in range(200)])
            self.assertEqual([message.content for message in latest], [str(index) for index in range(5, 205)])
            self.assertEqual(latest[-1].metadata["stream_id"], "stream-a")
            self.assertEqual(len(await backend.list_messages("owner-a", "thread-a", limit=500)), 205)
            # Exercise the actual GET handler with that same persisted boundary.
            self.namespace["_store"].return_value = backend
            self.namespace["_thread_payload"] = lambda record: record.to_payload()
            self.namespace["_message_payload"] = lambda record: record.to_payload()
            response = await self.get_thread()
            self.assertEqual(response["active_stream_ids"], [])
            self.assertEqual(response["messages"][-1]["metadata"]["stream_id"], "stream-a")
            self.assertNotIn("PRIVATE FOREIGN", str(response))

    async def test_postgres_recent_window_preserves_parameter_scoping_and_chronology(self):
        from unittest.mock import AsyncMock, patch
        from services.persistence.chat_store import PostgresChatBackend

        class ConnectionContext:
            async def __aenter__(self):
                return connection
            async def __aexit__(self, *_):
                return False

        connection = self.namespace_type(fetch=AsyncMock(return_value=[{"content": "new"}, {"content": "old"}]))
        pool = self.namespace_type(acquire=lambda: ConnectionContext())
        backend = PostgresChatBackend("postgresql://synthetic-unused")
        with patch.object(backend, "initialize", new=AsyncMock()), patch.object(backend, "_pg_pool", new=AsyncMock(return_value=pool)), patch("services.persistence.chat_store._message_from_row", side_effect=lambda row: row):
            result = await backend.list_messages("owner-a", "thread-a", newest=True)
        self.assertEqual([record["content"] for record in result], ["old", "new"])
        query, *parameters = connection.fetch.await_args.args
        self.assertIn("ORDER BY created_at DESC", query)
        self.assertEqual(parameters, ["owner-a", "thread-a", 200])
