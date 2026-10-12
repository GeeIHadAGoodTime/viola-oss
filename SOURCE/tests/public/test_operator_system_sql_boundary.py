"""Actual operator-system SQL admission, without database or credential I/O."""

# ruff: noqa: PT009, PT027 -- standalone stdlib unittest coverage

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core.db_backend import RlsContextRequiredError, _OperatorControlSystemConnection


class OperatorSystemSqlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.capability = _OperatorControlSystemConnection(None)

    def test_real_store_statements_are_admitted(self):
        path = Path(__file__).resolve().parents[2] / "services/operator_controls.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"_set_control_row", "asystem_audit_records"}:
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in {"fetch", "fetchrow", "execute"}
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)
                    ):
                        sql = call.args[0].value
                        self.capability._check(sql)
                        self.capability._check(sql.lower())
                        self.capability._check(
                            sql.replace("operator_controls", "public.operator_controls").replace(
                                "operator_control_audit", "public.operator_control_audit"
                            )
                        )
                        for suffix in ("; SELECT 1", " -- hidden", " /* hidden */", " UNION SELECT * FROM secrets"):
                            with self.subTest(sql=sql, suffix=suffix), self.assertRaises(RlsContextRequiredError):
                                self.capability._check(sql + suffix)
                        count += 1
        self.assertEqual(count, 4)

    def test_query_prefix_does_not_authorize_additional_relations(self):
        for sql in (
            "SELECT * FROM operator_controls JOIN app_user_profiles ON true",
            "SELECT * FROM operator_controls UNION SELECT * FROM app_user_profiles",
            "UPDATE operator_controls SET reason=(SELECT email FROM app_user_profiles LIMIT 1)",
            "INSERT INTO operator_control_audit SELECT * FROM private_audit",
        ):
            with self.subTest(sql=sql), self.assertRaises(RlsContextRequiredError):
                self.capability._check(sql)

    def test_query_prefix_does_not_authorize_role_or_subject_configuration(self):
        for target in ("role", "app.user_id", "search_path"):
            for sql in (
                "SELECT * FROM operator_controls WHERE set_config('%s','other',true) IS NOT NULL" % target,
                "UPDATE operator_controls SET reason=set_config('%s','other',true)" % target,
                "INSERT INTO operator_control_audit (audit_id) VALUES (set_config('%s','other',true))" % target,
            ):
                with self.subTest(sql=sql), self.assertRaises(RlsContextRequiredError):
                    self.capability._check(sql)

    def test_audit_reads_remain_null_only(self):
        self.capability._check("SELECT * FROM operator_control_audit WHERE user_id IS NULL")
        for predicate in ("user_id IS NOT NULL", "user_id IS NULL OR true", "user_id=$1"):
            with self.subTest(predicate=predicate), self.assertRaises(RlsContextRequiredError):
                self.capability._check("SELECT * FROM operator_control_audit WHERE " + predicate)

    def test_audit_history_and_role_changes_are_not_admitted(self):
        for sql in (
            "DELETE FROM operator_control_audit",
            "UPDATE operator_control_audit SET reason=$1",
            "SET LOCAL ROLE viola_app",
            "WITH x AS (DELETE FROM operator_control_audit RETURNING *) SELECT * FROM x",
        ):
            with self.subTest(sql=sql), self.assertRaises(RlsContextRequiredError):
                self.capability._check(sql)


if __name__ == "__main__":
    unittest.main()
