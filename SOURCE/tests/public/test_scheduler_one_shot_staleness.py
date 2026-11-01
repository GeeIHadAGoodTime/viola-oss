"""A one-shot action more than a day late is skipped and recorded, never fired late."""
from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

_METHODS = {"_check_and_fire", "_is_stale_one_shot", "_skip_stale_one_shot"}
_CONSTANTS = {"ONE_SHOT_STALE_AFTER_SECONDS", "ONE_SHOT_STALE_ERROR"}


def scheduler_type():
    source = Path(__file__).resolve().parents[2] / "services/scheduler/service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in _CONSTANTS for t in node.targets)]
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SchedulerService")
    methods = [node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in _METHODS]
    if len(methods) != len(_METHODS) or len(constants) != len(_CONSTANTS):
        raise AssertionError("scheduler staleness seam is missing")
    namespace = {"asyncio": asyncio, "datetime": datetime, "UTC": UTC,
                 "logger": logging.getLogger("scheduler-staleness-test"), "Schedule": object}
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *constants,
                            ast.ClassDef(name="SchedulerService", bases=[], keywords=[], body=methods,
                                         decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(source), "exec"), namespace)
    return namespace


class _Store:
    def __init__(self, due):
        self.due, self.recorded = due, []

    async def get_due_schedules_for_all_users_async(self, now_iso):
        return list(self.due)

    async def record_execution_async(self, user_id, schedule_id, status, next_run_at, error=None):
        self.recorded.append((user_id, schedule_id, status, next_run_at, error))


def _schedule(schedule_id, *, late, cron=None):
    due = (datetime.now(UTC) - late).isoformat()
    return SimpleNamespace(id=schedule_id, user_id="user-%d" % schedule_id, cron_expr=cron,
                           next_run_at=due, action="remind me", last_status=None, last_run_at=None)


class OneShotStaleness(unittest.IsolatedAsyncioTestCase):
    def build(self, due):
        namespace = scheduler_type()
        scheduler = namespace["SchedulerService"]()
        scheduler._store = _Store(due)
        scheduler._inflight_ids, scheduler._inflight_tasks, scheduler.fired = set(), set(), []
        scheduler._is_llm_healthy = lambda: True
        scheduler._should_backoff = lambda schedule: False
        scheduler._is_instant_command = lambda action: True

        async def execute(schedule):
            scheduler.fired.append(schedule.id)

        scheduler._guarded_execute = execute
        return namespace, scheduler

    async def test_one_shot_a_week_late_is_skipped_and_recorded_not_fired(self):
        namespace, scheduler = self.build([_schedule(1, late=timedelta(days=7))])
        await scheduler._check_and_fire()
        await asyncio.sleep(0)
        self.assertEqual(scheduler.fired, [])
        self.assertEqual(scheduler._store.recorded,
                         [("user-1", 1, "skipped", None, namespace["ONE_SHOT_STALE_ERROR"])])
        self.assertEqual(namespace["ONE_SHOT_STALE_AFTER_SECONDS"], 24 * 3600)

    async def test_one_shot_a_few_hours_late_still_fires_once(self):
        _namespace, scheduler = self.build([_schedule(2, late=timedelta(hours=3))])
        await scheduler._check_and_fire()
        await asyncio.sleep(0)
        self.assertEqual(scheduler.fired, [2])
        self.assertEqual(scheduler._store.recorded, [])

    async def test_recurring_action_a_week_late_fires_once_without_catch_up(self):
        _namespace, scheduler = self.build([_schedule(3, late=timedelta(days=7), cron="0 7 * * *")])
        await scheduler._check_and_fire()
        await asyncio.sleep(0)
        self.assertEqual(scheduler.fired, [3])
        self.assertEqual(scheduler._store.recorded, [])


if __name__ == "__main__":
    unittest.main()
