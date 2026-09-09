"""Local install identifier persistence for telemetry and update cohorts."""

from __future__ import annotations

import secrets
from typing import Any

_INSTALL_ID_PREFIX = "viola-install-"
_INSTALL_ID_BYTES = 18

# Machine-readable marker for a TEST install (our own activation-pipeline
# verifications), the install-id analog of the ``viola_test_account`` /
# reserved-test-domain rule for GoTrue accounts (CLAUDE.md "Testing"). A test
# install id is ``viola-install-testinstall`` + random suffix. The marker is
# 11 fixed lowercase chars immediately after the prefix, which ``generate_install_id``
# (a random ``token_urlsafe``) cannot reproduce except at ~1/64**11 odds, so the
# real ``funnel_first_run`` metric is computable by excluding this marker ALONE —
# never a curated denylist. The ingest routes a first_run carrying such an id to
# ``first_run_test`` so a test never inflates the real activation count.
_TEST_INSTALL_ID_MARKER = "testinstall"
_TEST_INSTALL_ID_PREFIX = _INSTALL_ID_PREFIX + _TEST_INSTALL_ID_MARKER


def generate_install_id() -> str:
    """Return a random, non-PII install identifier."""
    return _INSTALL_ID_PREFIX + secrets.token_urlsafe(_INSTALL_ID_BYTES)


def make_test_install_id() -> str:
    """Return a machine-readably-marked TEST install id (never a real user).

    Used only by activation-pipeline verifications. Shares the valid-install-id
    shape (so it survives ingest validation) but carries the reserved marker so
    the growth oracle can segregate it from real installs by the marker alone.
    """
    return _TEST_INSTALL_ID_PREFIX + secrets.token_urlsafe(_INSTALL_ID_BYTES)


def is_valid_install_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_INSTALL_ID_PREFIX) and len(value) >= 24


def is_test_install_id(value: Any) -> bool:
    """True for a reserved TEST install id (our verification pings), never a real one."""
    return isinstance(value, str) and value.startswith(_TEST_INSTALL_ID_PREFIX)


def get_or_create_install_id() -> str:
    """Return the stable machine install id, creating it in settings if needed."""
    try:
        from config.settings import settings as app_settings

        configured = getattr(app_settings, "telemetry_install_id", "")
    except ImportError:
        configured = ""
    if is_valid_install_id(configured):
        return configured

    from ui.settings_manager import get_settings_manager

    manager = get_settings_manager()
    current = manager.get("telemetry_install_id", "")
    if is_valid_install_id(current):
        return str(current)

    created = generate_install_id()
    manager.set("telemetry_install_id", created, save_immediately=True)
    return created
