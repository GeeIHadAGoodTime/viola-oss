"""Regression coverage for the legacy pre-call briefing helper."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from pathlib import Path
from unittest.mock import patch

from intent.approval import ApprovalManager
from intent.tool_types import RiskLevel
from telephony import call_tools
from telephony.call_tools import present_call_plan_handler


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _load_approval_bridge_without_package_side_effects():
    """Load the real bridge without importing the eager MCP package initializer."""

    package_name = "_phone_test_mcp_hub"
    package = ModuleType(package_name)
    package.__path__ = [str(SOURCE_ROOT / "mcp_hub")]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    module_name = f"{package_name}.approval_bridge"
    path = SOURCE_ROOT / "mcp_hub" / "approval_bridge.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _Params:
    def __init__(self) -> None:
        self.arguments = {
            "phone_number": "+12025550123",
            "business_name": "Example Business",
            "objective": "Run a synthetic confirmation check",
            "talking_points": [],
        }
        self.result: dict[str, Any] | None = None

    async def result_callback(self, result: dict[str, Any]) -> None:
        self.result = result


class PreCallPlanConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_call_briefing_uses_structured_approval_decision(self) -> None:
        """Negated or unclear replies must never authorize the planned call."""

        cases = [
            ("yes", "affirm", True),
            ("don't go ahead", "deny", False),
            ("I don't approve", "deny", False),
            ("do not dial", "deny", False),
            ("maybe", "unclear", False),
        ]
        for answer, decision, expected in cases:
            with self.subTest(answer=answer, decision=decision):
                ask_user_module = ModuleType("intent.tools.ask_user")

                async def ask_user_handler(_prompt: str, *, context: str) -> SimpleNamespace:
                    self.assertEqual(context, "pre-call briefing")
                    return SimpleNamespace(ok=True, data={"answer": answer})

                ask_user_module.ask_user_handler = ask_user_handler  # type: ignore[attr-defined]
                classified: list[tuple[str, str, RiskLevel]] = []

                async def classify(
                    _self: ApprovalManager,
                    response_text: str,
                    action_description: str,
                    risk: RiskLevel,
                ) -> str:
                    classified.append((response_text, action_description, risk))
                    return decision

                with (
                    patch.dict(sys.modules, {"intent.tools.ask_user": ask_user_module}),
                    patch.object(ApprovalManager, "_classify_response", classify),
                ):
                    params = _Params()
                    await present_call_plan_handler(params)

                self.assertEqual(params.result, {"approved": expected, "user_feedback": answer})
                self.assertEqual(
                    classified,
                    [(answer, "place the planned phone call to Example Business", RiskLevel.CONFIRM)],
                )

    async def test_phone_call_remains_dangerous_and_central_denial_blocks_dispatch(self) -> None:
        """The actual compound phone call path must retain its independent gate."""

        approval_bridge = _load_approval_bridge_without_package_side_effects()

        class DenialChannel:
            supports_buttons = False

            async def ask(self, _prompt: str, timeout: float | None = None) -> str:
                del timeout
                return "don't place the call"

            async def send(self, _message: str) -> None:
                return None

        async def classify(
            _self: ApprovalManager,
            _response_text: str,
            _action_description: str,
            _risk: RiskLevel,
        ) -> str:
            return "deny"

        bridge = approval_bridge.ApprovalBridge(ApprovalManager(channel=DenialChannel()))
        args = {
            "action": "call",
            "phone_number": "+12025550123",
            "task": "Synthetic only; no phone tool is invoked",
        }

        # ApprovalManager imports this module again for its immutable-risk-map
        # invariant. Bind the already-loaded real module at that precise boundary
        # so Python does not execute mcp_hub/__init__.py and import GUI backends.
        with (
            patch.dict(sys.modules, {"mcp_hub.approval_bridge": approval_bridge}),
            patch.object(ApprovalManager, "_classify_response", classify),
        ):
            self.assertEqual(bridge.get_call_risk("phone", args), RiskLevel.DANGEROUS)
            self.assertFalse(await bridge.check_approval("phone", args))


class ConferenceUserResolutionTests(unittest.TestCase):
    def test_conference_aliases_are_generic_only(self) -> None:
        """The source distribution must not embed an individual owner's names."""

        self.assertEqual(
            call_tools._CONFERENCE_USER_ALIASES,
            {
                "",
                "i",
                "me",
                "myself",
                "user",
                "the user",
                "caller",
                "the caller",
                "founder",
                "the founder",
            },
        )

    def test_conference_generic_alias_uses_only_authenticated_user_settings(self) -> None:
        """A generic alias resolves per-user settings without a global fallback."""

        class SettingsManager:
            settings = {"user_phone_number": "+12025550199"}

            def get(self, key: str, default: str, *, user_id: str) -> str:
                self_test.assertEqual(user_id, "authenticated-user")
                self_test.assertEqual(default, "")
                return "+12025550123" if key == "user_phone_number" else ""

        self_test = self
        settings_module = ModuleType("ui.settings_manager")
        settings_module.get_settings_manager = lambda: SettingsManager()  # type: ignore[attr-defined]

        self.assertFalse(call_tools._can_use_global_conference_settings("authenticated-user"))
        # Number normalization is a separate unit. Patch that boundary so this
        # identity/settings regression does not import the eager intent.tools
        # package and its optional Windows GUI backend.
        with (
            patch.dict(sys.modules, {"ui.settings_manager": settings_module}),
            patch.object(call_tools, "_normalize_us_e164", side_effect=lambda value: value),
        ):
            self.assertEqual(
                call_tools._resolve_conference_user_phone("authenticated-user", "me"),
                "+12025550123",
            )

    def test_conference_authenticated_user_without_phone_does_not_fall_back(self) -> None:
        """Missing per-user settings for an authenticated user must fail closed."""

        class SettingsManager:
            settings = {"user_phone_number": "+12025550199"}

            def get(self, _key: str, default: str, *, user_id: str) -> str:
                self_test.assertEqual(user_id, "authenticated-user")
                return default

        self_test = self
        settings_module = ModuleType("ui.settings_manager")
        settings_module.get_settings_manager = lambda: SettingsManager()  # type: ignore[attr-defined]

        self.assertFalse(call_tools._can_use_global_conference_settings("authenticated-user"))
        with (
            patch.dict(sys.modules, {"ui.settings_manager": settings_module}),
            self.assertRaisesRegex(ValueError, "Set user_phone_number"),
        ):
            call_tools._resolve_conference_user_phone("authenticated-user", "me")
