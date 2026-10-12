"""Execute the shipped lifecycle and pump bodies with synthetic carrier/queue IO."""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

SOURCE = Path(__file__).resolve().parents[2] / "telephony/call_manager.py"


def runtime():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CallManager")
    methods = [node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {"__init__", "_schedule_queue_pump", "_start_next_queued_call_if_idle"}]
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"register_call_manager_observer", "unregister_call_manager_observer", "_notify_call_manager_observers"}]
    namespace = {"asyncio": asyncio, "os": SimpleNamespace(getenv=lambda _name: None),
                 "_call_manager_observers": [], "logger": logging.getLogger("phone-queue-lifecycle-test"),
                 "NumberPool": lambda **_kwargs: object(), "create_recording_storage": lambda **_kwargs: object(),
                 "_register_call_manager_shutdown_hooks": lambda manager: namespace["managers"].add(manager),
                 "managers": weakref.WeakSet()}
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                           *functions, ast.ClassDef(name="CallManager", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(SOURCE), "exec"), namespace)
    return namespace


class Queue:
    def __init__(self):
        self.rows = [SimpleNamespace(queue_id=name, user_id="synthetic", user_tier="pro", phone_number="synthetic",
                                     task="synthetic", caller_name="Synthetic", extra_context="", max_duration=None,
                                     issuer_channel_info={}) for name in ("already-claimed", "not-started")]
        self.claimed = asyncio.Event()
        self.finish_claim = asyncio.Event()
        self.pops = 0

    async def pop_next(self):
        self.pops += 1
        item = self.rows.pop(0) if self.rows else None
        if item and item.queue_id == "already-claimed":
            self.claimed.set()
            await self.finish_claim.wait()
        return item

    async def enqueue(self, item):
        self.rows.append(item)


def construct(scope, queue):
    config = SimpleNamespace(is_configured=True, phone_numbers=[], phone_number="synthetic", recording_storage="local",
                             recording_s3_bucket="", recording_s3_endpoint="", recording_s3_prefix="")
    module = ModuleType("telephony.config")
    module.load_recording_secrets = lambda: {}
    with patch.dict(sys.modules, {"telephony.config": module}):
        return scope["CallManager"](config, queue_store=queue)


class PhoneQueueLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_observer_parks_before_any_first_claim(self):
        scope, queue = runtime(), Queue()
        scope["register_call_manager_observer"](lambda manager: setattr(manager, "_accounting_cutover_queue_paused", True))
        manager = construct(scope, queue)
        await asyncio.sleep(0)
        self.assertIsNone(manager._queue_pump_task)
        self.assertEqual(queue.pops, 0)
        self.assertEqual(len(queue.rows), 2)
        self.assertIn(manager, scope["managers"])

    async def test_existing_claim_finishes_but_next_row_is_not_consumed(self):
        scope, queue = runtime(), Queue()
        # Constructor is parked; configure synthetic launch IO before resuming.
        scope["register_call_manager_observer"](lambda manager: setattr(manager, "_accounting_cutover_queue_paused", True))
        manager = construct(scope, queue)
        started = []
        manager._active_outbound_call_count = lambda: len(started)
        manager._max_active_outbound_calls = lambda: 2
        manager._user_has_outbound_capacity = lambda *_args: True
        async def launched(**kwargs):
            started.append(kwargs["task"])
        async def broadcast(_user):
            return None
        manager._make_call_unqueued = launched
        manager._broadcast_call_queue_update = broadcast
        manager._accounting_cutover_queue_paused = False
        manager._schedule_queue_pump()
        await queue.claimed.wait()
        manager._accounting_cutover_queue_paused = True
        queue.finish_claim.set()
        await manager._queue_pump_task
        self.assertEqual(started, ["synthetic"])
        self.assertEqual(queue.pops, 1)
        self.assertEqual([row.queue_id for row in queue.rows], ["not-started"])
        self.assertFalse(manager._queue_pump_lock.locked())

    async def test_waiting_pump_rechecks_pause_before_pop(self):
        scope, queue = runtime(), Queue()
        scope["register_call_manager_observer"](lambda manager: setattr(manager, "_accounting_cutover_queue_paused", True))
        manager = construct(scope, queue)
        manager._accounting_cutover_queue_paused = False
        await manager._queue_pump_lock.acquire()
        manager._schedule_queue_pump()
        await asyncio.sleep(0)
        manager._accounting_cutover_queue_paused = True
        manager._queue_pump_lock.release()
        await manager._queue_pump_task
        self.assertEqual(queue.pops, 0)
        self.assertEqual(len(queue.rows), 2)

    async def test_observer_failure_propagates_before_scheduling(self):
        scope, queue = runtime(), Queue()
        def failed(_manager):
            raise RuntimeError("private lifecycle unavailable")
        scope["register_call_manager_observer"](failed)
        with self.assertRaisesRegex(RuntimeError, "lifecycle unavailable"):
            construct(scope, queue)
        await asyncio.sleep(0)
        self.assertEqual(queue.pops, 0)

    async def test_unregister_is_scoped_and_does_not_release_an_existing_pause(self):
        scope, queue = runtime(), Queue()
        observed = []
        def first(manager):
            manager._accounting_cutover_queue_paused = True
        def second(manager):
            observed.append(manager)
        scope["register_call_manager_observer"](first)
        scope["register_call_manager_observer"](first)
        scope["register_call_manager_observer"](second)
        manager = construct(scope, queue)
        scope["unregister_call_manager_observer"](second)
        scope["unregister_call_manager_observer"](second)
        self.assertTrue(manager._accounting_cutover_queue_paused)
        self.assertEqual(observed, [manager])
        self.assertEqual(scope["_call_manager_observers"], [first])


if __name__ == "__main__":
    unittest.main()
