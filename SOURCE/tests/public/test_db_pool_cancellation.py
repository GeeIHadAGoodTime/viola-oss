"""Actual pool-wrapper lifetimes with controlled awaits and no database calls."""

# ruff: noqa: PT009, PT027 -- standalone stdlib unittest coverage

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from core import db_backend

OWNER = "00000000-0000-0000-0000-000000000001"


class ControlledRawPool:
    """Plain managers model asyncpg without async-generator GC cleanup."""

    def __init__(self, *blocked):
        self.blocked = set(blocked)
        self.entered = {
            phase: asyncio.Event() for phase in ("begin", "context", "body", "rollback", "commit", "release")
        }
        self.finish = {phase: asyncio.Event() for phase in self.entered}
        self.interrupted = set()
        self.failures = {}
        self.acquired = self.released = self.transaction_started = self.transaction_finished = 0
        self.active = 0
        self.context = None
        self.in_transaction = False
        self.writes = []

    async def pause(self, phase):
        self.entered[phase].set()
        if phase in self.blocked:
            try:
                await self.finish[phase].wait()
            except asyncio.CancelledError:
                self.interrupted.add(phase)
                raise
        if phase in self.failures:
            raise self.failures[phase]

    def acquire(self, *args, **kwargs):
        raw = self

        class Acquire:
            async def __aenter__(self):
                if raw.active:
                    raise RuntimeError("Synthetic one-slot pool exhausted")
                raw.acquired += 1
                raw.active += 1
                return raw

            async def __aexit__(self, *_args):
                await raw.pause("release")
                raw.active -= 1
                raw.released += 1
                # asyncpg resets or terminates the connection before returning
                # its holder, including an interrupted BEGIN's uncertain state.
                raw.context = None
                raw.in_transaction = False

        return Acquire()

    def transaction(self):
        raw = self

        class Transaction:
            async def __aenter__(self):
                raw.transaction_started += 1
                raw.in_transaction = True
                await raw.pause("begin")
                return self

            async def __aexit__(self, exc_type, *_args):
                await raw.pause("rollback" if exc_type is not None else "commit")
                raw.transaction_finished += 1
                raw.in_transaction = False
                raw.context = None

        return Transaction()

    async def execute(self, query, *args):
        if query == "SELECT set_config('app.user_id', $1, true)":
            assert self.in_transaction and args == (OWNER,)
            await self.pause("context")
            self.context = args[0]
            return "SELECT 1"
        self.writes.append(query)
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        assert query == "SELECT 1" and self.context == OWNER
        return 1

    async def fetch(self, *_args):
        return [{"schemaname": "public", "tablename": "sync_chat_threads"}]


class DriverTransactionPool(ControlledRawPool):
    """Real asyncpg Transaction state machine with synthetic SQL transport."""

    def __init__(self, *blocked):
        from types import SimpleNamespace

        super().__init__(*blocked)
        self._pool_release_ctr = 0
        self._top_xact = None
        self._protocol = SimpleNamespace(is_in_transaction=lambda: self.in_transaction)
        self.queries = []
        self.isolation = "read committed"
        self._unique_id = 0

    def is_closed(self):
        return False

    def _get_unique_id(self, prefix):
        self._unique_id += 1
        return "__%s_%d__" % (prefix, self._unique_id)

    def transaction(self, *, isolation=None, readonly=False, deferrable=False):
        from asyncpg.transaction import Transaction

        return Transaction(self, isolation, readonly, deferrable)

    def acquire(self, *args, **kwargs):
        assert "transaction_options" not in kwargs
        manager = super().acquire(*args, **kwargs)
        raw = self

        class Acquire:
            async def __aenter__(self):
                return await manager.__aenter__()

            async def __aexit__(self, *args):
                await manager.__aexit__(*args)
                raw._top_xact = None
                raw._pool_release_ctr += 1

        return Acquire()

    async def execute(self, query, *args):
        self.queries.append((query, args))
        if query.startswith("BEGIN"):
            self.isolation = "repeatable read" if "REPEATABLE READ" in query else "read committed"
            self.transaction_started += 1
            self.in_transaction = True
            await self.pause("begin")
        elif query.startswith("SAVEPOINT"):
            self.transaction_started += 1
        elif query.startswith("ROLLBACK TO"):
            await self.pause("rollback")
            self.transaction_finished += 1
        elif query.startswith("RELEASE SAVEPOINT"):
            await self.pause("commit")
            self.transaction_finished += 1
        elif query in ("COMMIT;", "ROLLBACK;"):
            await self.pause("commit" if query == "COMMIT;" else "rollback")
            self.transaction_finished += 1
            self.in_transaction = False
            self.context = None
        else:
            return await super().execute(query, *args)
        return "OK"

    async def fetchval(self, query, *args):
        if query == "SHOW transaction_isolation;":
            return self.isolation
        return await super().fetchval(query, *args)


class PoolCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enabled = patch.object(db_backend, "_db_user_context_enabled", return_value=True)
        self.enabled.start()
        self.token = db_backend.set_db_user_id(OWNER)

    async def asyncTearDown(self):
        db_backend.reset_db_user_id(self.token)
        self.enabled.stop()

    async def usable_again(self, pool, raw):
        self.assertEqual(raw.acquired, raw.released)
        self.assertEqual(raw.active, 0)
        self.assertIsNone(raw.context)
        self.assertFalse(raw.in_transaction)
        raw.blocked.clear()
        raw.failures.clear()
        async with pool.acquire() as connection:
            self.assertEqual(await connection.fetchval("SELECT 1"), 1)
        self.assertEqual(raw.acquired, raw.released)
        self.assertIsNone(raw.context)

    async def cancel_request_at(self, phase):
        raw = ControlledRawPool(phase)
        pool = db_backend.ResilientAsyncpgPool(raw)
        yielded = False

        async def request():
            nonlocal yielded
            async with pool.acquire() as connection:
                yielded = True
                await raw.pause("body")
                self.assertEqual(await connection.fetchval("SELECT 1"), 1)

        task = asyncio.create_task(request())
        await asyncio.wait_for(raw.entered[phase].wait(), 1)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertEqual(yielded, phase == "body")
            self.assertEqual(raw.released, 1)
            self.assertEqual(raw.transaction_finished, 0 if phase == "begin" else 1)
            await self.usable_again(pool, raw)
        finally:
            raw.finish[phase].set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_during_transaction_start_releases_and_resets(self):
        await self.cancel_request_at("begin")

    async def test_cancel_during_rls_setup_rolls_back_releases_and_recovers(self):
        await self.cancel_request_at("context")

    async def test_cancel_after_yield_rolls_back_releases_and_recovers(self):
        await self.cancel_request_at("body")

    async def interrupt_cleanup(self, phase, *, fail_body=False):
        raw = ControlledRawPool(phase)
        pool = db_backend.ResilientAsyncpgPool(raw)

        async def request():
            async with pool.acquire():
                if fail_body:
                    raise ValueError("Synthetic body refusal")

        task = asyncio.create_task(request())
        await asyncio.wait_for(raw.entered[phase].wait(), 1)
        try:
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "Cancellation must wait for connection cleanup")
            self.assertNotIn(phase, raw.interrupted)
        finally:
            raw.finish[phase].set()
            await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(raw.transaction_finished, 1)
        self.assertEqual(raw.released, 1)
        await self.usable_again(pool, raw)

    async def test_repeated_cancellation_during_rollback_joins_cleanup(self):
        await self.interrupt_cleanup("rollback", fail_body=True)

    async def test_repeated_cancellation_during_commit_joins_cleanup(self):
        await self.interrupt_cleanup("commit")

    async def test_repeated_cancellation_during_release_joins_cleanup(self):
        await self.interrupt_cleanup("release")

    async def test_repeated_cancellation_while_setup_failure_rolls_back(self):
        raw = ControlledRawPool("context", "rollback")
        pool = db_backend.ResilientAsyncpgPool(raw)

        async def request():
            async with pool.acquire():
                self.fail("Interrupted RLS setup must never yield a connection")

        task = asyncio.create_task(request())
        await asyncio.wait_for(raw.entered["context"].wait(), 1)
        task.cancel()
        await asyncio.wait_for(raw.entered["rollback"].wait(), 1)
        try:
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertNotIn("rollback", raw.interrupted)
        finally:
            raw.finish["rollback"].set()
            await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(raw.transaction_finished, 1)
        self.assertEqual(raw.released, 1)
        await self.usable_again(pool, raw)

    async def test_rls_setup_refusal_never_yields_or_runs_user_sql(self):
        raw = ControlledRawPool()
        raw.failures["context"] = ValueError("Synthetic RLS setup refusal")
        pool = db_backend.ResilientAsyncpgPool(raw)
        with self.assertRaisesRegex(ValueError, "RLS setup refusal"):
            async with pool.acquire() as connection:
                await connection.execute("UPDATE sync_chat_threads SET title = 'unsafe'")
        self.assertEqual(raw.writes, [])
        self.assertEqual(raw.transaction_finished, 1)
        await self.usable_again(pool, raw)

    async def test_rollback_error_still_releases_and_propagates(self):
        raw = ControlledRawPool()
        raw.failures["rollback"] = RuntimeError("Synthetic rollback failure")
        pool = db_backend.ResilientAsyncpgPool(raw)
        with self.assertRaisesRegex(RuntimeError, "rollback failure"):
            async with pool.acquire():
                raise ValueError("Synthetic request failure")
        self.assertEqual(raw.released, 1)
        await self.usable_again(pool, raw)

    async def test_rollback_cancellation_still_releases_and_propagates(self):
        raw = ControlledRawPool()
        raw.failures["rollback"] = asyncio.CancelledError()
        pool = db_backend.ResilientAsyncpgPool(raw)
        with self.assertRaises(asyncio.CancelledError):
            async with pool.acquire():
                raise ValueError("Synthetic request failure")
        self.assertEqual(raw.released, 1)
        await self.usable_again(pool, raw)

    async def test_no_ambient_owner_keeps_tenant_write_guard_fail_closed(self):
        raw = ControlledRawPool()
        pool = db_backend.ResilientAsyncpgPool(raw)
        with (
            patch.object(db_backend, "_connection_db_user_id", return_value=None),
            patch.object(db_backend, "_rls_guarded_tables", {}),
        ):
            with self.assertRaises(db_backend.RlsContextRequiredError):
                async with pool.acquire() as connection:
                    await connection.execute("UPDATE sync_chat_threads SET title = 'unsafe'")
        self.assertEqual(raw.writes, [])
        self.assertEqual(raw.released, 1)
        self.assertEqual(raw.transaction_started, 0)

    async def test_setup_cancellation_remains_cancellation_if_rollback_fails(self):
        raw = ControlledRawPool("context")
        raw.failures["rollback"] = RuntimeError("Synthetic rollback failure")
        pool = db_backend.ResilientAsyncpgPool(raw)

        async def request():
            async with pool.acquire():
                self.fail("Interrupted setup must not yield a connection")

        task = asyncio.create_task(request())
        await asyncio.wait_for(raw.entered["context"].wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(raw.released, 1)
        await self.usable_again(pool, raw)

    async def test_stale_setup_retry_releases_failed_handle_before_retry(self):
        raw = ControlledRawPool()
        pool = db_backend.ResilientAsyncpgPool(raw)
        original = raw.execute

        class SyntheticStaleHandle(RuntimeError):
            pass

        async def execute(query, *args):
            if raw.acquired == 1:
                raise SyntheticStaleHandle("Synthetic stale connection")
            return await original(query, *args)

        raw.execute = execute
        with patch.object(
            db_backend, "_is_asyncpg_connection_error", side_effect=lambda exc: isinstance(exc, SyntheticStaleHandle)
        ):
            async with pool.acquire() as connection:
                self.assertEqual(await connection.fetchval("SELECT 1"), 1)
        self.assertEqual(raw.acquired, 2)
        self.assertEqual(raw.released, 2)
        self.assertEqual(raw.transaction_finished, 2)

    async def cancel_with_failed_cleanup(self, phase):
        raw = ControlledRawPool()
        original_acquire = raw.acquire
        if phase == "rollback":
            raw.failures["rollback"] = RuntimeError("Synthetic rollback failure")
        else:

            def acquire():
                manager = original_acquire()

                class FailingRelease:
                    async def __aenter__(self):
                        return await manager.__aenter__()

                    async def __aexit__(self, *args):
                        await manager.__aexit__(*args)
                        raise RuntimeError("Synthetic release failure after reset")

                return FailingRelease()

            raw.acquire = acquire
        pool = db_backend.ResilientAsyncpgPool(raw)
        with self.assertRaises(asyncio.CancelledError) as failure:
            async with pool.acquire():
                raise asyncio.CancelledError("Synthetic request deadline")
        self.assertEqual(str(failure.exception), "Synthetic request deadline")
        self.assertIsInstance(failure.exception.__cause__, RuntimeError)
        self.assertIn(phase, str(failure.exception.__cause__))
        self.assertEqual(raw.released, 1)
        raw.acquire = original_acquire
        await self.usable_again(pool, raw)

    async def test_request_cancellation_survives_rollback_error(self):
        await self.cancel_with_failed_cleanup("rollback")

    async def test_request_cancellation_survives_release_error(self):
        await self.cancel_with_failed_cleanup("release")

    async def test_snapshot_options_reach_actual_driver_outer_transaction_before_rls(self):
        raw = DriverTransactionPool()
        pool = db_backend.ResilientAsyncpgPool(raw)
        options = {"isolation": "repeatable_read", "readonly": True}
        manager = pool.acquire(transaction_options=options)
        options["isolation"] = "serializable"  # Caller mutation cannot alter an admitted manager.
        async with manager as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                self.assertEqual(await connection.fetchval("SELECT 1"), 1)
        self.assertEqual(raw.queries[0][0], "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")
        self.assertEqual(raw.queries[1], ("SELECT set_config('app.user_id', $1, true)", (OWNER,)))
        self.assertTrue(raw.queries[2][0].startswith("SAVEPOINT"))
        self.assertTrue(raw.queries[-2][0].startswith("RELEASE SAVEPOINT"))
        self.assertEqual(raw.queries[-1][0], "COMMIT;")
        self.assertEqual(raw.acquired, raw.released)
        self.assertEqual(raw.transaction_started, raw.transaction_finished)

    async def test_snapshot_options_without_owner_do_not_bypass_rls(self):
        raw = ControlledRawPool()
        pool = db_backend.ResilientAsyncpgPool(raw)
        with (
            patch.object(db_backend, "_connection_db_user_id", return_value=None),
            patch.object(db_backend, "_rls_guarded_tables", {}),
        ):
            with self.assertRaises(db_backend.RlsContextRequiredError):
                async with pool.acquire(
                    transaction_options={"isolation": "repeatable_read", "readonly": True}
                ) as connection:
                    await connection.execute("UPDATE sync_chat_threads SET title = 'unsafe'")
        self.assertEqual(raw.writes, [])
        self.assertEqual(raw.released, 1)
        self.assertEqual(raw.transaction_started, 0)


if __name__ == "__main__":
    unittest.main()
