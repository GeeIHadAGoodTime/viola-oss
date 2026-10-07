"""Desktop relays delegate the monthly meter but keep command spend bounded."""

from __future__ import annotations

import ast
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

budget = None
LlmSpendReservation = None
LlmTokenUsage = None
estimated_spend_cents = None
_profile = None
_profile_env = None
_home_lookup = None


def setUpModule():
    """All incidental config/log initialization stays in a disposable profile."""
    global budget, LlmSpendReservation, LlmTokenUsage, estimated_spend_cents
    global _profile, _profile_env, _home_lookup
    _profile = tempfile.TemporaryDirectory(prefix="viola-remote-accounting-")
    profile = Path(_profile.name)
    _profile_env = patch.dict(os.environ, {"VIOLA_DATA_DIR": str(profile / "data"),
        "VIOLA_CACHE_DIR": str(profile / "cache"), "VIOLA_LOG_DIR": str(profile / "logs")})
    _home_lookup = patch.object(Path, "home", return_value=profile / "home")
    _profile_env.start()
    _home_lookup.start()
    from services.llm import managed_budget, spend_accounting

    budget = managed_budget
    LlmSpendReservation = spend_accounting.LlmSpendReservation
    LlmTokenUsage = spend_accounting.LlmTokenUsage
    estimated_spend_cents = spend_accounting.estimated_spend_cents


def tearDownModule():
    profile = Path(_profile.name).resolve()
    loggers = [logging.getLogger(), *[logger for logger in logging.Logger.manager.loggerDict.values()
                                     if isinstance(logger, logging.Logger)]]
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve().is_relative_to(profile):
                logger.removeHandler(handler)
                handler.close()
    _home_lookup.stop()
    _profile_env.stop()
    _profile.cleanup()


class RemoteManagedAccounting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = types.ModuleType("config.settings")
        self.settings.settings = SimpleNamespace(app_surface="desktop")
        self.billing = types.ModuleType("billing.managed_llm_budget")
        self.denied = budget.ManagedLlmBudgetGate(allowed=False, spent_cents=17, budget_cents=17,
                                                period="monthly", plan="max")
        self.billing.check_managed_llm_spend_cap = Mock(return_value=self.denied)
        self.billing.check_managed_llm_spend_cap_async = AsyncMock(return_value=self.denied)
        self.billing.reserve_managed_llm_spend_cap_async = AsyncMock(return_value=self.denied)
        self.remote = SimpleNamespace(MANAGED_SPEND_ACCOUNTED_REMOTELY=True)
        self.patch = patch.dict(sys.modules, {"config.settings": self.settings,
                                              "billing.managed_llm_budget": self.billing})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_remote_provider_is_not_blocked_by_an_exhausted_static_local_max_meter(self):
        assert budget.check_managed_llm_spend_cap("paid", managed_llm=True, provider=self.remote).allowed
        assert (await budget.check_managed_llm_spend_cap_async("paid", managed_llm=True, provider=self.remote)).allowed
        assert (await budget.reserve_managed_llm_spend_cap_async("paid", managed_llm=True, provider=self.remote)).allowed
        self.billing.check_managed_llm_spend_cap.assert_not_called()
        self.billing.check_managed_llm_spend_cap_async.assert_not_called()
        self.billing.reserve_managed_llm_spend_cap_async.assert_not_called()

    async def test_desktop_alone_direct_keys_and_browser_forged_capability_do_not_bypass(self):
        for provider in (None, SimpleNamespace(), {"MANAGED_SPEND_ACCOUNTED_REMOTELY": True},
                         SimpleNamespace(MANAGED_SPEND_ACCOUNTED_REMOTELY="true"), Mock()):
            gate = await budget.check_managed_llm_spend_cap_async("paid", managed_llm=True, provider=provider)
            assert not gate.allowed
            assert gate.budget_cents == 17
        assert self.billing.check_managed_llm_spend_cap_async.await_count == 5
        assert "budget_cents" not in self.denied.cap_state

    async def test_cloud_retains_authoritative_monthly_gate_even_for_a_marked_provider(self):
        self.settings.settings.app_surface = "cloud"
        gate = await budget.reserve_managed_llm_spend_cap_async("paid", managed_llm=True, provider=self.remote)
        assert not gate.allowed
        self.billing.reserve_managed_llm_spend_cap_async.assert_awaited_once()

    async def test_remote_reservation_never_debits_or_settles_the_desktop_monthly_store(self):
        reservation = LlmSpendReservation(user_id="paid", model="gpt-6-luna",
            estimated_usage=LlmTokenUsage(1_000_000, 1_000_000), operation="desktop-relay", reserve_tokens=False,
            provider=self.remote)
        with patch.object(budget, "user_uses_managed_llm", return_value=True):
            await reservation.reserve()
            await reservation.settle(LlmTokenUsage(1_000_000, 1_000_000))
        assert reservation.remotely_accounted
        assert reservation._spend_reservation is None
        self.billing.reserve_managed_llm_spend_cap_async.assert_not_called()
        assert reservation._settled

    def test_remote_command_guard_keeps_its_independent_ceiling(self):
        private = types.ModuleType("billing.per_command_spend_guard")
        private.build_per_command_spend_guard = Mock(return_value=SimpleNamespace(cap_cents=300))
        with patch.dict(sys.modules, {private.__name__: private}):
            guard = budget.build_per_command_spend_guard("paid", managed_llm=True, provider=self.remote)
        assert guard.cap_cents == 300
        private.build_per_command_spend_guard.assert_called_once_with("paid", managed_llm=True, remotely_accounted=True)

    def test_selected_router_transport_matches_request_snapshot(self):
        source = ROOT / "services/llm/provider_router.py"
        owner = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(node, ast.ClassDef)
                     and node.name == "ProviderAgnosticRouter")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                      and node.name == "MANAGED_SPEND_ACCOUNTED_REMOTELY")
        method.decorator_list = []
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), namespace)
        router = SimpleNamespace(_sync_runtime_provider_if_needed=lambda: None,
                                 _provider_from_frozen_or_current=lambda: self.remote)
        assert namespace[method.name](router)
        router._provider_from_frozen_or_current = lambda: SimpleNamespace()
        assert not namespace[method.name](router)

    async def test_remote_turn_usage_advances_command_guard_without_a_local_hold(self):
        source = ROOT / "intent/agent_loop.py"
        owner = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == "run_agent_loop")
        method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_settle_agent_turn_spend_success")
        guard = SimpleNamespace(spent_cents=0, cap_cents=300)
        guard.record = lambda cents: setattr(guard, "spent_cents", guard.spent_cents + cents)
        usage = LlmTokenUsage(0, 6_000_000)
        namespace = {"Any": object, "_per_command_spend_guard": guard,
                     "_agent_turn_model_name": lambda *args: "gpt-6-luna",
                     "_actual_agent_turn_usage": lambda *args: usage,
                     "_handle_agent_loop_billing_failure": lambda exc: False}
        wrapper = ast.FunctionDef(name="bind_settlement", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
            kw_defaults=[], defaults=[]), body=[ast.Assign(targets=[ast.Name(id="outcome", ctx=ast.Store())],
            value=ast.Constant("success")), method, ast.Return(value=ast.Name(id=method.name, ctx=ast.Load()))], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(source), "exec"), namespace)
        namespace[method.name] = namespace["bind_settlement"]()
        reservation = SimpleNamespace(remotely_accounted=True, _spend_reservation=None, settle=AsyncMock())
        await namespace[method.name](reservation=reservation, estimated_usage=usage,
                                     response={"_usage": {"output_tokens": 6_000_000}}, turn_kwargs={})
        assert guard.spent_cents == estimated_spend_cents("gpt-6-luna", usage)
        assert guard.spent_cents == 300
        guard.spent_cents = 0
        await namespace[method.name](reservation=reservation, estimated_usage=usage,
                                     response={"_managed_spend_accounted_remotely": False}, turn_kwargs={})
        assert guard.spent_cents == 0

    def test_cloud_cap_error_carries_safe_state_without_provider_cost_dollars(self):
        error = budget.ManagedLlmSpendCapError("Included usage reached", {"plan": "max", "period": "monthly",
            "percent_used": 100, "resets_at": "2026-11-07T00:00:00Z", "spent_cents": 17,
            "limit_cents": 17, "extra_usage_cents": 3, "purchase_url": "https://untrusted.invalid"})
        assert error.public_message == "Included usage reached"
        assert error.limit_type == "managed_llm_spend_cap"
        assert error.cap_state["percent_used"] == 100
        assert error.cap_state["purchase_url"] == "/billing/capacity"
        assert not {"spent_cents", "limit_cents", "extra_usage_cents"} & error.cap_state.keys()
        assert budget.cap_state_from_response({"data": {"cap_state": error.cap_state}}) == error.cap_state

    async def test_policy_answer_projects_state_before_stopping_the_agent(self):
        source = ROOT / "intent/agent_loop.py"
        owner = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_agent_loop")
        method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_settle_agent_turn_spend_success")
        wrapper = ast.FunctionDef(name="bind_settlement", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
            kw_defaults=[], defaults=[]), body=[ast.Assign(targets=[ast.Name(id="outcome", ctx=ast.Store())],
            value=ast.Constant("success")), method, ast.Return(value=ast.Name(id=method.name, ctx=ast.Load()))], decorator_list=[])
        stop = type("CostLimitStop", (Exception,), {})
        executor = SimpleNamespace(_final_params={})
        settle = AsyncMock()
        namespace = {"Any": object, "executor": executor, "_AgentLoopCostLimitStop": stop,
            "_settle_agent_turn_spend_failed": settle,
            "_set_final_response": lambda owner, answer, **kwargs: setattr(owner, "answer", answer)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(source), "exec"), namespace)
        stopped = False
        try:
            await namespace["bind_settlement"]()(reservation=object(), estimated_usage=None, turn_kwargs={},
                response={"_policy_denial": True, "error": "managed_llm_budget_unavailable", "answer": "Try again.",
                    "cap_state": {"denial_code": "managed_llm_budget_unavailable", "remaining_cents": 99,
                        "purchase_url": "https://untrusted.invalid", "diagnostic_id": "not-a-valid-id"}})
        except stop:
            stopped = True
        assert stopped
        state = executor._final_params["cap_state"]
        assert state["denial_code"] == "managed_llm_budget_unavailable"
        assert executor._final_params["stop_reason"] == "managed_llm_budget_unavailable"
        assert state["purchase_url"] == "/billing/capacity"
        assert "remaining_cents" not in state and "diagnostic_id" not in state
        settle.assert_awaited_once()

    def test_positive_balance_is_not_described_as_exhausted(self):
        gate = budget.ManagedLlmBudgetGate(False, spent_cents=8, budget_cents=33, plan="free",
            period="weekly", estimated_cents=26, reason="weekly managed spend cap reached")
        message = budget.managed_llm_budget_message(gate)
        assert "larger than your remaining" in message
        assert "reached" not in message and "cents" not in message
        assert gate.cap_state["denial_code"] == "managed_llm_spend_cap"
        assert not {"spent_cents", "limit_cents", "estimated_cents", "remaining_cents"} & gate.cap_state.keys()

    async def test_non_budget_denial_keeps_its_type_and_does_not_claim_exhaustion(self):
        cases = [("budget counter unavailable", "managed_llm_budget_unavailable"),
                 ("account capacity unavailable", "managed_llm_budget_unavailable"),
                 ("authenticated managed-LLM user required", "authenticated_user_required"),
                 ("owner safety control unavailable", "owner_safety_control_disabled")]
        from core.exceptions import LLMQuotaExceededError
        for reason, code in cases:
            gate = budget.ManagedLlmBudgetGate(False, reason=reason)
            reservation = LlmSpendReservation(user_id="synthetic", model="gpt-6-luna",
                estimated_usage=LlmTokenUsage(100, 100), operation="synthetic-test", reserve_tokens=False)
            with patch.object(budget, "user_uses_managed_llm", return_value=True), patch.object(
                budget, "reserve_managed_llm_spend_cap_async", new=AsyncMock(return_value=gate)):
                caught = None
                try:
                    await reservation.reserve()
                except LLMQuotaExceededError as error:
                    caught = error
            assert caught is not None
            assert caught.limit_type == code
            assert caught.cap_state["denial_code"] == code
            assert "reached" not in caught.public_message

    def test_reported_zero_and_partial_usage_preserve_provider_truth(self):
        from services.llm.spend_accounting import usage_from_openai_usage, usage_source_from_openai_usage
        fallback = LlmTokenUsage(100, 200)
        zero = usage_from_openai_usage({"input_tokens": 0, "output_tokens": 0}, fallback)
        assert (zero.input_tokens, zero.output_tokens) == (0, 0)
        result = usage_from_openai_usage({"input_tokens": 0, "output_tokens": None}, fallback)
        assert (result.input_tokens, result.output_tokens) == (0, 200)
        assert usage_source_from_openai_usage({"input_tokens": 0, "output_tokens": 0}) == "provider"
        assert usage_source_from_openai_usage({"input_tokens": 0}) == "mixed"
        assert usage_source_from_openai_usage({}) == "estimate"

    async def test_terminal_cloud_denial_preserves_work_and_reaches_the_cap_notice_contract(self):
        source = ROOT / "intent/agent_loop.py"
        loop = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_agent_loop")
        method = next(node for node in loop.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_handle_spend_reservation_denial")
        wrapper = ast.FunctionDef(name="bind_handler", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
            kw_defaults=[], defaults=[]), body=[ast.Assign(targets=[ast.Name(id="outcome", ctx=ast.Store())],
            value=ast.Constant("success")), method, ast.Return(value=ast.Name(id=method.name, ctx=ast.Load()))], decorator_list=[])
        stop = type("CostLimitStop", (Exception,), {})
        executor = SimpleNamespace(_final_params={}, _record_agent_gate_denial_telemetry=Mock(),
                                   _managed_llm_budget_message=budget.managed_llm_budget_message)
        namespace = {"Any": object, "executor": executor, "_AgentLoopCostLimitStop": stop,
                     "_set_final_response": lambda owner, answer, **kwargs: setattr(owner, "answer", answer)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(source), "exec"), namespace)
        handler = namespace["bind_handler"]()
        error = budget.ManagedLlmSpendCapError("Add more usage to continue", {"plan": "max", "period": "monthly",
            "percent_used": 100, "resets_at": "2026-11-07T00:00:00Z"})
        halted = False
        try:
            await handler(None, error)
        except stop:
            halted = True
        assert halted
        assert executor.answer == "Add more usage to continue"
        assert executor._final_params["cap_state"]["period"] == "monthly"
        assert executor._final_params["cap_state"]["percent_used"] == 100
        assert not {"spent_cents", "limit_cents"} & executor._final_params["cap_state"].keys()


if __name__ == "__main__":
    unittest.main()
