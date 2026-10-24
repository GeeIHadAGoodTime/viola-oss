"""Shared route guards for Viola API endpoints."""

from __future__ import annotations

import os

from fastapi import HTTPException


def require_dev_mode() -> None:
    """Raise 404 if dev_mode is not enabled.

    Use as a FastAPI dependency::

        @router.get("/endpoint", dependencies=[Depends(require_dev_mode)])
        async def handler(): ...
    """
    from config.settings import settings

    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="Not Found")


def require_dev_or_test_context() -> None:
    """Raise 404 unless the process is running in a dev/test context.

    Looser gate than ``require_dev_mode``: also honors the standard Viola
    dev/test signals so the payment-confirm regression harness (tier 30,
    PAY-001/005/006/007/008/009) can exercise the test-only session
    endpoints without requiring operators to flip ``VIOLA_DEV_MODE=1``.

    Unlocked when ANY of the following is true:

    * ``settings.dev_mode`` is True (explicit ``VIOLA_DEV_MODE`` toggle)
    * ``settings.test_mode`` is True (explicit ``VIOLA_TEST_MODE`` toggle)
    * source checkout only: ``settings.env`` is ``"dev"`` or ``"test"``
      (default on fresh checkout)
    * source checkout only: ``VIOLA_TEST_BYPASS_LIMITS=1`` is set in the
      environment (used by the regression harness and the billing-bypass
      path in billing/plan_limiter.py)

    Installed builds (``config.settings.is_release_build()``) also run with
    ``env="dev"``, because nothing sets ``VIOLA_ENV`` on a customer's
    machine. So on a release build only the two explicit toggles unlock
    these routes; the ``env`` default and the bypass variable never do.

    In ``env="prod"`` the VIOLA_TEST_BYPASS_LIMITS escape hatch is
    ignored — this guard matches the plan_limiter bypass contract,
    which also refuses to honor VIOLA_TEST_BYPASS_LIMITS outside dev.
    """
    from config.settings import is_release_build, settings

    if settings.dev_mode:
        return
    if getattr(settings, "test_mode", False):
        return
    if is_release_build():
        raise HTTPException(status_code=404, detail="Not Found")
    env_name = getattr(settings, "env", "")
    if env_name in ("dev", "test"):
        return
    # VIOLA_TEST_BYPASS_LIMITS=1 only unlocks in non-prod environments
    # (same contract as billing/plan_limiter.py:166-175).
    if env_name != "prod" and os.environ.get("VIOLA_TEST_BYPASS_LIMITS") == "1":
        return

    raise HTTPException(status_code=404, detail="Not Found")
