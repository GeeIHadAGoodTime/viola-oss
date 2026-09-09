#!/usr/bin/env python3
"""
Viola launcher and security utility CLI.

Default behaviour launches the primary Qt desktop client. Additional
flags support security administration tasks such as rotating API keys.
"""

from __future__ import annotations

import argparse
import sys

from core.platform import configure_environment

configure_environment()

# CRITICAL: Load .env file FIRST before any other imports
# This ensures OPENAI_API_KEY and other env vars are available
import config.settings
from core.console import console
from core.logging_config import get_logger
from services.sentry_init import init_sentry

init_sentry("run_viola")

logger = get_logger(__name__)


def _reset_auth() -> int:
    from ui.security.bootstrap import rotate_bootstrap_api_key

    info = rotate_bootstrap_api_key()
    logger.info("Rotated Viola API key. New key saved to %s", info.path)
    console(info.api_key, flush=True)
    return 0


def _show_auth_key() -> int:
    from ui.security.bootstrap import ensure_bootstrap_api_key

    info = ensure_bootstrap_api_key()
    logger.info("Current Viola API key located at %s", info.path)
    console(info.api_key, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Viola launcher and security utility")
    parser.add_argument(
        "--reset-auth",
        action="store_true",
        help="Rotate the Viola API key and print the new value.",
    )
    parser.add_argument(
        "--show-auth-key",
        action="store_true",
        help="Print the current Viola API key without rotating it.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Pass through to the Qt launcher to run the headless daemon.",
    )

    args = parser.parse_args(argv)

    if args.reset_auth and args.show_auth_key:
        parser.error("Flags --reset-auth and --show-auth-key cannot be used together.")

    if args.reset_auth:
        return _reset_auth()

    if args.show_auth_key:
        return _show_auth_key()

    from viola_qt import main as qt_main

    if args.headless:
        sys.argv = [sys.argv[0], "--headless"]
    qt_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
