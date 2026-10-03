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
