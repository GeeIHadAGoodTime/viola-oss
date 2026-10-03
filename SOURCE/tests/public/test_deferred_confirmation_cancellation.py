"""Synthetic deferred-approval lifecycle regressions; no real tools or providers."""

from __future__ import annotations

import unittest
import uuid
import sys
from unittest.mock import patch

from core.user_context import user_scope
from intent.approval import ApprovalManager, ConfirmationDeferred, _PENDING_STORE_REGISTRY
from intent.tool_types import RiskLevel


class DeferredCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = "synthetic-" + uuid.uuid4().hex
        self.scope = user_scope(self.owner)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.addCleanup(_PENDING_STORE_REGISTRY.pop, self.owner, None)
        self.manager = ApprovalManager()
        self.effects = 0
        self.args = {"target": "synthetic-counter"}

    async def dispatch(self, args, task, *, manager=None):
        manager = manager or self.manager
        try:
            approved = await manager.request_approval(
                "increment synthetic counter",
                RiskLevel.DANGEROUS,
                "synthetic_mutation",
                dict(args),
                task,
            )
        except ConfirmationDeferred as deferred:
            return "pending", deferred.confirmation_id
        if approved:
            self.effects += 1
        return "success" if approved else "denied", None

    async def pending(self):
        status, cid = await self.dispatch(self.args, "turn-1")
        self.assertEqual(status, "pending")
        self.assertEqual(self.effects, 0)
        return cid

    def decision(self, cid, confirmed, **args):
        return {**self.args, **args, "_approval_confirmation_id": cid, "_approval_confirmed": confirmed}

    async def test_exact_cancel_removes_pending_and_retry_requires_fresh_approval(self):
        cid = await self.pending()
        self.assertEqual(await self.dispatch(self.decision(cid, False), "turn-2"), ("denied", None))
        self.assertEqual(self.effects, 0)
        self.assertEqual(self.manager.pending_confirmations_for_prompt(), [])
        # Neither explicit replay nor a native-schema reissue may use the cancelled grant.
        self.assertEqual(await self.dispatch(self.decision(cid, True), "turn-3"), ("denied", None))
        status, fresh = await self.dispatch(self.args, "turn-4")
        self.assertEqual(status, "pending")
        self.assertNotEqual(fresh, cid)
        self.assertEqual(self.effects, 0)
        self.assertEqual(await self.dispatch(self.decision(fresh, True), "turn-5"), ("success", None))
        self.assertEqual(self.effects, 1)

    async def test_cancel_does_not_remove_another_action(self):
        cid = await self.pending()
        _, other = await self.dispatch({"target": "other-counter"}, "turn-other")
        await self.dispatch(self.decision(cid, False), "turn-2")
        self.assertEqual([p["confirmation_id"] for p in self.manager.pending_confirmations_for_prompt()], [other])

    async def test_wrong_owner_cannot_cancel_or_consume_pending(self):
        cid = await self.pending()
        stranger = "synthetic-" + uuid.uuid4().hex
        self.addCleanup(_PENDING_STORE_REGISTRY.pop, stranger, None)
        with user_scope(stranger):
            self.assertEqual(await self.dispatch(self.decision(cid, False), "stranger-1"), ("denied", None))
            self.assertEqual(await self.dispatch(self.decision(cid, True), "stranger-2"), ("denied", None))
            self.assertEqual(self.manager.pending_confirmations_for_prompt(), [])
        self.assertEqual(len(self.manager.pending_confirmations_for_prompt()), 1)
        self.assertEqual(await self.dispatch(self.decision(cid, True), "turn-2"), ("success", None))

    async def test_wrong_args_or_tool_or_risk_cannot_cancel_pending(self):
        cid = await self.pending()
        self.assertEqual(
            await self.dispatch(self.decision(cid, False, target="other-counter"), "turn-2"), ("denied", None)
        )
        for tool, risk in [("other_tool", RiskLevel.DANGEROUS), ("synthetic_mutation", RiskLevel.CONFIRM)]:
            state = self.manager.consume_deferred_confirmation(
                tool_name=tool,
                risk=risk,
                tool_args=self.decision(cid, False),
                task_id="turn-3",
            )
            self.assertEqual(state, "rejected")
        self.assertEqual(len(self.manager.pending_confirmations_for_prompt()), 1)

    async def test_malformed_decision_does_not_cancel_or_approve(self):
        cid = await self.pending()
        for malformed in [None, "false", "true", 0, 1]:
            self.assertEqual(await self.dispatch(self.decision(cid, malformed), "turn-2"), ("denied", None))
        self.assertEqual(len(self.manager.pending_confirmations_for_prompt()), 1)
        self.assertEqual(self.effects, 0)

    async def test_cancel_from_same_owner_other_device_invalidates_original_manager(self):
        cid = await self.pending()
        self.assertEqual(
            await self.dispatch(self.decision(cid, False), "turn-2", manager=ApprovalManager()), ("denied", None)
        )
        self.assertEqual(self.manager.pending_confirmations_for_prompt(), [])
        self.assertEqual((await self.dispatch(self.args, "turn-3"))[0], "pending")
        self.assertEqual(self.effects, 0)

    async def test_affirmative_other_device_is_one_shot(self):
        cid = await self.pending()
        self.assertEqual(
            await self.dispatch(self.decision(cid, True), "turn-2", manager=ApprovalManager()), ("success", None)
        )
        self.assertEqual(await self.dispatch(self.decision(cid, True), "turn-3"), ("denied", None))
        self.assertEqual(self.effects, 1)

    async def test_same_turn_cannot_authorize_itself(self):
        cid = await self.pending()
        self.assertEqual(await self.dispatch(self.decision(cid, True), "turn-1"), ("denied", None))
        self.assertEqual((await self.dispatch(self.args, "turn-1"))[0], "pending")
        self.assertEqual(self.effects, 0)

    async def test_expired_confirmation_requires_new_prompt(self):
        with patch("intent.approval.time.monotonic", return_value=100):
            cid = await self.pending()
        with patch("intent.approval.time.monotonic", return_value=401):
            self.assertEqual(await self.dispatch(self.decision(cid, True), "turn-2"), ("denied", None))
            self.assertEqual((await self.dispatch(self.args, "turn-3"))[0], "pending")
        self.assertEqual(self.effects, 0)

    async def test_native_schema_confirmed_reissue_remains_supported(self):
        await self.pending()
        self.assertEqual(await self.dispatch(self.args, "turn-2", manager=ApprovalManager()), ("success", None))
        self.assertEqual(self.effects, 1)

    async def test_real_bridge_preserves_deny_cancel_and_fresh_retry(self):
        from tests.public.test_phone_call_plan_confirmation import (
            _load_approval_bridge_without_package_side_effects,
        )

        bridge_module = _load_approval_bridge_without_package_side_effects()
        bridge = bridge_module.ApprovalBridge(self.manager, user_id=self.owner, session_id="synthetic-session")
        tool = "synthetic__counter"
        self.assertEqual(bridge.get_call_risk(tool, self.args), RiskLevel.DANGEROUS)
        with (
            patch.dict(sys.modules, {"mcp_hub.approval_bridge": bridge_module}),
            patch.object(bridge_module, "_active_agent_task_id", return_value="turn-1") as active_task,
        ):
            with self.assertRaises(ConfirmationDeferred) as pending:
                await bridge.check_approval(tool, dict(self.args))
            cid = pending.exception.confirmation_id
            active_task.return_value = "turn-2"
            self.assertFalse(await bridge.check_approval(tool, self.decision(cid, False)))
            self.assertEqual(bridge.last_permission_decision.behavior, "deny")
            self.assertEqual(self.manager.pending_confirmations_for_prompt(), [])
            active_task.return_value = "turn-3"
            with self.assertRaises(ConfirmationDeferred) as fresh:
                await bridge.check_approval(tool, dict(self.args))
            self.assertNotEqual(fresh.exception.confirmation_id, cid)
            active_task.return_value = "turn-4"
            approved = await bridge.check_approval(tool, self.decision(fresh.exception.confirmation_id, True))
            if approved:
                self.effects += 1
            self.assertTrue(approved)
            self.assertEqual(self.effects, 1)


if __name__ == "__main__":
    unittest.main()
