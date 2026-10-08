"""Exercise the registered desktop key action, with an inert CDP boundary.

Run in a subprocess so full server imports cannot contaminate other public
contracts. No browser, user session, permission state, or network is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BrowserKeyActionContract(unittest.TestCase):
    def test_registered_desktop_key_action(self):
        with tempfile.TemporaryDirectory() as state:
            env = dict(
                os.environ,
                PYTHONPATH=str(ROOT),
                VIOLA_CDP_PORT="0",
                XDG_DATA_HOME=state,
            )
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--isolated"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def isolated_contracts():
    sys.path.insert(0, str(ROOT))
    import asyncio
    import socket
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    def no_network(*args, **kwargs):
        raise AssertionError("The CDP contract must not connect to a real browser or network")

    socket.create_connection = no_network

    from mcp_servers.browser_cdp import server as mod
    from services.playwright_cdp_client import PlaywrightCDPCommandTimeoutError

    # Windows asyncio creates a loopback socketpair for its event loop. Block
    # browser connection at the transport boundary rather than that loop setup.
    mod.PlaywrightCDPClient.connect = AsyncMock(side_effect=no_network)

    class RegisteredKeyAction(unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            self.client = SimpleNamespace(
                connected=True,
                get_url=AsyncMock(return_value="https://example.test/form"),
                press_key=AsyncMock(),
                type_text=AsyncMock(),
                evaluate_js=AsyncMock(
                    return_value={
                        "tag": "input",
                        "inputType": "text",
                        "name": "Search",
                        "url": "https://example.test",
                    }
                ),
                close=AsyncMock(),
                page=SimpleNamespace(wait_for_load_state=AsyncMock()),
            )
            self.manager = mod.CDPBrowserManager()
            self.manager._cdp = self.client
            self.enterContext(patch.object(mod, "manager", self.manager))
            mod._clear_cdp_refs()
            self.addCleanup(mod._clear_cdp_refs)

        async def call(self, **arguments):
            response = await mod.server.call_tool("browser_interact", {"action": "press_key", **arguments})
            blocks = response[0] if isinstance(response, tuple) else response
            return json.loads(blocks[0].text)

        def assert_no_input(self):
            self.client.press_key.assert_not_awaited()
            self.client.type_text.assert_not_awaited()

        async def test_exposed_schema_and_confirmation_metadata(self):
            tools = {tool.name: tool for tool in await mod.server.list_tools()}
            self.assertNotIn("browser_press_key", tools)
            tool = tools["browser_interact"]
            self.assertIn("key", tool.inputSchema["properties"])
            self.assertIn("press_key", tool.inputSchema["properties"]["action"]["description"])
            self.assertFalse(tool.annotations.readOnlyHint)
            self.assertEqual(tool.meta["risk"], "confirm")

        async def test_supported_named_keys_reach_cdp(self):
            for key, canonical in [
                ("Enter", "Enter"),
                ("RETURN", "Enter"),
                ("tab", "Tab"),
                ("Escape", "Escape"),
                ("ArrowDown", "ArrowDown"),
                ("Backspace", "Backspace"),
                ("Delete", "Delete"),
                ("Space", " "),
                (" ", " "),
            ]:
                with self.subTest(key=key):
                    result = await self.call(key=key)
                    self.assertEqual(result, {"pressed": key})
                    self.client.press_key.assert_awaited_with(canonical)
            self.client.type_text.assert_not_awaited()

        async def test_single_character_preserves_existing_insert_contract(self):
            self.assertEqual(await self.call(key="a"), {"pressed": "a"})
            self.client.type_text.assert_awaited_once_with("a")
            self.client.press_key.assert_not_awaited()

        async def test_missing_key_is_actionable_and_has_no_side_effect(self):
            result = await self.call()
            self.assertIn("key is required", result["error"])
            self.assertFalse(result["ok"])
            self.client.get_url.assert_not_awaited()
            self.assert_no_input()

        async def test_unsupported_keys_never_become_typed_text(self):
            for key in ["Control+A", "NotAKey", "EnterEnter", "\n", "\t", "\x00", "  "]:
                with self.subTest(key=key):
                    result = await self.call(key=key)
                    self.assertIn("Unsupported key", result["error"])
                    self.assertFalse(result["ok"])
            self.client.get_url.assert_not_awaited()
            self.assert_no_input()

        async def test_target_arguments_are_not_silently_ignored(self):
            for arguments in [
                {"selector": "#submit"},
                {"text": "hello"},
                {"value": "option"},
            ]:
                result = await self.call(key="Enter", **arguments)
                self.assertIn("currently focused element", result["error"])
            self.assert_no_input()

        async def test_payment_submission_stays_blocked_in_registered_path(self):
            self.client.evaluate_js.return_value = {
                "tag": "button",
                "inputType": "submit",
                "text": "Pay now",
                "name": "Pay now",
                "url": "https://shop.test/checkout",
            }
            for key in ["Enter", "RETURN", "Space", " "]:
                result = await self.call(key=key)
                self.assertFalse(result["ok"])
                self.assertIn("PAYMENT", result["error"])
            self.assert_no_input()

        async def test_single_character_cannot_fill_payment_fields(self):
            self.client.evaluate_js.return_value = {
                "tag": "input",
                "inputType": "text",
                "name": "Card number",
                "ariaLabel": "Card number",
                "nameAttr": "card_number",
                "formText": "Payment method. Card number. Expiry. CVV.",
                "url": "https://shop.test/checkout",
            }
            for key in ["4", "İ", "Space", " "]:
                result = await self.call(key=key)
                self.assertFalse(result["ok"])
                self.assertIn("PAYMENT", result["error"])
            self.assert_no_input()

        async def test_single_character_cannot_fill_signature_fields(self):
            self.client.evaluate_js.return_value = {
                "tag": "input",
                "inputType": "text",
                "name": "Legal signature",
                "pageText": "By entering your name you certify this is a legal signature.",
                "url": "https://state.test/signature",
            }
            result = await self.call(key="J")
            self.assertFalse(result["ok"])
            self.assertIn("SIGNATURE", result["error"])
            self.assert_no_input()

        async def test_character_field_inspection_failure_is_closed(self):
            self.client.evaluate_js.side_effect = RuntimeError("frame detached")
            for key in ["4", "Enter", "Space"]:
                result = await self.call(key=key)
                self.assertFalse(result["ok"])
                self.assertIn("focused element", result["error"])
            self.assert_no_input()

        async def test_signature_submission_stays_blocked_in_registered_path(self):
            self.client.evaluate_js.return_value = {
                "tag": "input",
                "inputType": "checkbox",
                "text": "Legal signature",
                "pageText": "Checking this box constitutes a legal signature.",
                "url": "https://state.test/signature",
            }
            for key in ["Enter", "RETURN", "Space", " "]:
                result = await self.call(key=key)
                self.assertFalse(result["ok"])
                self.assertIn("SIGNATURE", result["error"])
            self.assert_no_input()

        async def test_signature_approved_resume_still_reaches_existing_guard(self):
            self.client.evaluate_js.return_value = {
                "tag": "input",
                "inputType": "checkbox",
                "text": "Legal signature",
                "pageText": "Checking this box constitutes a legal signature.",
                "url": "https://state.test/signature",
            }
            with patch.object(mod, "_consume_signature_gate_override", return_value=True) as consume:
                self.assertEqual(await self.call(key="Enter"), {"pressed": "Enter"})
            consume.assert_called_once()
            self.client.press_key.assert_awaited_once_with("Enter")

        async def test_navigation_really_clears_old_snapshot_refs(self):
            self.client.get_url.side_effect = [
                "https://example.test/form",
                "https://example.test/done",
            ]
            mod._CDP_REF_SELECTOR_MAP["e1"] = "#old"
            mod._CDP_REF_LOCATOR_MAP["e1"] = {"role": "button", "name": "Old"}
            result = await self.call(key="Enter")
            self.assertEqual(result["navigated_to"], "https://example.test/done")
            self.assertTrue(result["refs_invalidated"])
            self.assertEqual(result["ref_invalidation_reason"], "key_navigated")
            self.assertEqual(mod._CDP_REF_SELECTOR_MAP, {})
            self.assertEqual(mod._CDP_REF_LOCATOR_MAP, {})

        async def test_no_navigation_retains_refs_and_makes_no_effect_claim(self):
            mod._CDP_REF_SELECTOR_MAP["e1"] = "#same"
            result = await self.call(key="Enter")
            self.assertEqual(result, {"pressed": "Enter"})
            self.assertEqual(mod._CDP_REF_SELECTOR_MAP, {"e1": "#same"})

        async def test_dispatch_failure_is_reported_without_retry(self):
            self.client.press_key.side_effect = RuntimeError("Target page is closed")
            result = await self.call(key="Enter")
            self.assertIn("Target page is closed", result["error"])
            self.assertNotIn("pressed", result)
            self.client.press_key.assert_awaited_once()

        async def test_timeout_preserves_recovery_circuit(self):
            self.client.press_key.side_effect = PlaywrightCDPCommandTimeoutError("Keyboard.press", "timed out")
            result = await self.call(key="Enter")
            self.assertIn("error", result)
            self.client.close.assert_awaited_once()
            retry = await self.call(key="Enter")
            self.assertIn("recovering", retry["error"])
            self.client.press_key.assert_awaited_once()

        async def test_load_wait_timeout_does_not_repeat_dispatch(self):
            self.client.page.wait_for_load_state.side_effect = TimeoutError("no navigation")
            self.assertEqual(await self.call(key="Enter"), {"pressed": "Enter"})
            self.client.press_key.assert_awaited_once_with("Enter")

        async def test_disabled_browser_reports_configuration_blocker(self):
            from config.settings import settings

            self.manager._cdp = None
            with patch.object(settings, "cdp_port", 0):
                result = await self.call(key="Enter")
            self.assertIn("switched off", result["error"])
            self.assertIn("no website was contacted", result["error"])
            self.assert_no_input()

        async def test_cancellation_does_not_become_success(self):
            self.client.press_key.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await self.call(key="Enter")
            self.client.press_key.assert_awaited_once()

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegisteredKeyAction)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        raise SystemExit(isolated_contracts())
    unittest.main()
