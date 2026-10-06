"""Actual session-ordering helpers on tiny owned SQLite rows; no auth/runtime imports."""

from __future__ import annotations

import ast
import sqlite3
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_ordering_helpers(root: Path = ROOT):
    core = ast.parse((root / "core/user_context.py").read_text(encoding="utf-8"))
    auth = ast.parse((root / "auth/desktop_session.py").read_text(encoding="utf-8"))
    core_names = {"_desktop_session_rows_by_recent_use", "_parse_sqlite_datetime"}
    auth_names = {"_row_from_sqlite", "_parse_dt", "_nonempty_str"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [n for n in core.body if isinstance(n, ast.FunctionDef) and n.name in core_names]
    nodes += [n for n in auth.body if isinstance(n, ast.FunctionDef) and n.name in auth_names]
    nodes += [n for n in auth.body if isinstance(n, ast.ClassDef) and n.name == "_SessionRow"]
    store = next(n for n in auth.body if isinstance(n, ast.ClassDef) and n.name == "DesktopSessionStore")
    method = next(n for n in store.body if isinstance(n, ast.FunctionDef) and n.name == "_session_rows_by_recent_use")
    nodes.append(ast.ClassDef(name="BoundStore", bases=[], keywords=[], body=[method], decorator_list=[]))
    namespace = {"__name__": __name__, "datetime": datetime, "UTC": UTC, "sqlite3": sqlite3, "dataclass": dataclass}
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<actual-session-source>", "exec"),
        namespace,
    )
    return namespace["_desktop_session_rows_by_recent_use"], namespace["BoundStore"]._session_rows_by_recent_use


class OwnedRows:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE desktop_sessions (
                session_hash TEXT PRIMARY KEY, user_id TEXT, email TEXT,
                email_verified INTEGER, gotrue_session_id TEXT, created_at TEXT,
                expires_at TEXT, last_used_at TEXT, access_expires_at TEXT
            )
        """)

    def _ensure_sqlite_schema(self):
        pass

    def _connect(self):
        return self.connection

    def add(self, user: str, used: str, created: str | None = None):
        self.connection.execute(
            "INSERT INTO desktop_sessions VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (
                user,
                user,
                user + "@example.invalid",
                user,
                created or used,
                "2099-01-01T00:00:00+00:00",
                used,
                "2099-01-01T00:00:00+00:00",
            ),
        )


class DesktopSessionOrderingTests(unittest.TestCase):
    def setUp(self):
        self.core_order, self.store_order = load_ordering_helpers()
        self.rows = OwnedRows()
        self.addCleanup(self.rows.connection.close)

    def assert_orders(self, expected):
        before = list(self.rows.connection.iterdump())
        assert [r["user_id"] for r in self.core_order(self.rows)] == expected
        assert [r.user_id for r in self.store_order(self.rows)] == expected
        assert list(self.rows.connection.iterdump()) == before, "ordering must not mutate session rows"

    def test_every_subsecond_precision_selects_the_newer_account(self):
        for early, late in [("000001", "000002"), ("001000", "002000"), ("100000", "900000")]:
            with self.subTest(early=early, late=late):
                self.rows.connection.execute("DELETE FROM desktop_sessions")
                self.rows.add("first", f"2026-10-05T12:00:00.{early}+00:00")
                self.rows.add("second", f"2026-10-05T12:00:00.{late}+00:00")
                self.assert_orders(["second", "first"])

    def test_last_use_precedes_creation_order(self):
        self.rows.add("recently-used", "2026-10-05T12:00:00.900000+00:00", "2026-10-04T12:00:00+00:00")
        self.rows.add("recently-created", "2026-10-05T12:00:00.100000+00:00", "2026-10-05T12:00:00+00:00")
        self.assert_orders(["recently-used", "recently-created"])

    def test_creation_retains_microseconds_when_last_use_is_equal(self):
        used = "2026-10-05T12:00:01+00:00"
        self.rows.add("first", used, "2026-10-05T12:00:00.000001+00:00")
        self.rows.add("second", used, "2026-10-05T12:00:00.000002+00:00")
        self.assert_orders(["second", "first"])

    def test_offsets_compare_instants_rather_than_text(self):
        self.rows.add("offset-earlier", "2026-10-05T15:00:00.100000+03:00")
        self.rows.add("utc-later", "2026-10-05T12:00:00.200000+00:00")
        self.assert_orders(["utc-later", "offset-earlier"])

    def test_utc_naive_and_z_timestamps_retain_existing_parser_support(self):
        self.rows.add("no-fraction", "2026-10-05T12:00:00Z")
        self.rows.add("naive", "2026-10-05T12:00:00.100000")
        self.rows.add("fraction-z", "2026-10-05T12:00:00.200000Z")
        self.assert_orders(["fraction-z", "naive", "no-fraction"])

    def test_day_and_year_boundaries_still_sort_descending(self):
        self.rows.add("old", "2025-12-31T23:59:59.999999+00:00")
        self.rows.add("new", "2026-01-01T00:00:00+00:00")
        self.assert_orders(["new", "old"])

    def test_equal_timestamps_keep_stable_input_order(self):
        stamp = "2026-10-05T12:00:00.100000+00:00"
        self.rows.add("first", stamp)
        self.rows.add("second", stamp)
        self.assert_orders(["first", "second"])

    def test_malformed_timestamps_keep_existing_rejection_behavior(self):
        self.rows.add("malformed", "not-a-time")
        self.rows.add("valid", "2026-10-05T12:00:00+00:00")
        assert [r["user_id"] for r in self.core_order(self.rows)] == ["valid", "malformed"]
        try:
            self.store_order(self.rows)
        except ValueError:
            pass
        else:
            raise AssertionError("The store must retain its invalid-timestamp rejection")

    def test_unrepresentable_utc_offsets_cannot_hide_valid_rows(self):
        for value in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            for field in ("last-used", "created"):
                with self.subTest(value=value, field=field):
                    self.rows.connection.execute("DELETE FROM desktop_sessions")
                    valid = "2026-10-05T12:00:00+00:00"
                    self.rows.add("extreme", value if field == "last-used" else valid, value)
                    self.rows.add("valid", valid)
                    self.assert_orders(["valid", "extreme"])

    def test_unusable_ordering_value_ranks_below_valid_datetime_min(self):
        self.rows.add("extreme", "0001-01-01T00:00:00+01:00")
        self.rows.add("valid-minimum", "0001-01-01T00:00:00+00:00")
        self.assert_orders(["valid-minimum", "extreme"])

    def test_empty_session_store_remains_empty(self):
        self.assert_orders([])


if __name__ == "__main__":
    unittest.main()
