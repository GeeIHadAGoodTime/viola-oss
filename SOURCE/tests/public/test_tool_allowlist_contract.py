"""Behavior contracts for explicit tool allowlists."""

import unittest

from intent.tool_allowlist import tool_name_allowed_by_allowlist


class ToolAllowlistContract(unittest.TestCase):
    def test_explicit_wildcard_and_named_restriction_preserve_their_meaning(self):
        for name in ("web_search", "mcp__search__web_search", "search.web_search"):
            with self.subTest(name=name):
                self.assertTrue(tool_name_allowed_by_allowlist({"*"}, name))
                self.assertTrue(tool_name_allowed_by_allowlist({"web_search"}, name))
        self.assertFalse(tool_name_allowed_by_allowlist({"web_search"}, "browser_click"))
