"""User email normalisation must not depend on split_email's tuple length."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from auth import models


class EmailValidatorTupleShapeTests(unittest.TestCase):
    def test_four_value_split_email_still_works(self) -> None:
        self.assertEqual(models._normalize_user_email("Owner@example.com"), "Owner@example.com")

    def test_longer_split_email_tuple_is_accepted(self) -> None:
        # email-validator 2.4.0 returns (display, original_local, local, quoted, domain).
        def longer(raw: str):
            local, _, domain = raw.partition("@")
            return (None, local, local, False, domain)

        with patch.object(models, "split_email", longer):
            self.assertEqual(models._normalize_user_email("owner@example.com"), "owner@example.com")


if __name__ == "__main__":
    unittest.main()
