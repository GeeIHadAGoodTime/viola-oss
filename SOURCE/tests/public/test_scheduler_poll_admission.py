"""Execute the shipped scheduler poll seam with retained synthetic due work."""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import unittest


def scheduler_type():
    source = Path(__file__).resolve().parents[2] / "services/scheduler/service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SchedulerService")
    methods = [node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {"set_poll_admission_factory", "_check_loop"}]
    namespace = {"asyncio": asyncio, "logger": logging.getLogger("scheduler-poll-admission-test")}
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                           ast.ClassDef(name="SchedulerService", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(source), "exec"), namespace)
    value = namespace["SchedulerService"]()
    value._running, value._check_task, value._poll_admission_factory = False, None, None
    return value


class SchedulerPollAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_default_poll_keeps_existing_behavior(self):
        scheduler, called = scheduler_type(), []
        async def poll():
            called.append("original")
        async def tick():
            scheduler._running = False
        scheduler._check_and_fire, scheduler._wait_for_next_tick = poll, tick
        scheduler._running = True
        await scheduler._check_loop()
        self.assertEqual(called, ["original"])

    async def test_parked_poll_never_reads_or_consumes_due_work(self):
        scheduler, due = scheduler_type(), ["retained-action"]
        admit, reached = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def admission():
            reached.set()
            await admit.wait()
            yield
        async def poll():
            due.pop()
        async def tick():
            scheduler._running = False
        scheduler.set_poll_admission_factory(admission)
        scheduler._check_and_fire, scheduler._wait_for_next_tick = poll, tick
        scheduler._running = True
        worker = asyncio.create_task(scheduler._check_loop())
        await reached.wait()
        self.assertEqual(due, ["retained-action"])
        self.assertFalse(worker.done())
        admit.set()
        await worker
        self.assertEqual(due, [])

    async def test_already_admitted_poll_finishes_without_cancellation(self):
        scheduler, entered, finish = scheduler_type(), asyncio.Event(), asyncio.Event()
        events = []
        @asynccontextmanager
        async def admission():
            events.append("admitted")
            try:
                yield
            finally:
                events.append("released")
        async def poll():
            entered.set()
            await finish.wait()
            events.append("original-finished")
        async def tick():
            scheduler._running = False
        scheduler.set_poll_admission_factory(admission)
        scheduler._check_and_fire, scheduler._wait_for_next_tick = poll, tick
        scheduler._running = True
        worker = asyncio.create_task(scheduler._check_loop())
        await entered.wait()
        self.assertEqual(events, ["admitted"])
        finish.set()
        await worker
        self.assertEqual(events, ["admitted", "original-finished", "released"])

    def test_admission_cannot_be_changed_after_scheduler_start(self):
        scheduler = scheduler_type()
        scheduler._running = True
        with self.assertRaises(RuntimeError):
            scheduler.set_poll_admission_factory(lambda: None)

    def test_noncallable_admission_is_rejected(self):
        scheduler = scheduler_type()
        with self.assertRaises(TypeError):
            scheduler.set_poll_admission_factory(object())
