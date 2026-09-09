"""
Migrate messaging tokens from settings.json / SecureSettingsManager to user_credentials DB table.

On first run for a local (desktop) install, reads any configured messaging bot
tokens from SettingsManager and writes them into the ``user_credentials`` table
under the local user. After migration, the tokens are removed from
settings.json (replaced with empty string) so only the DB copy remains.

Usage:
    python -m auth.migrations.migrate_messaging_tokens [--dry-run] [--user-id USER_ID]

The migration is idempotent: tokens already in the DB are skipped.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger("viola.auth.migrations.messaging_tokens")

# Mapping: settings key -> service name in user_credentials table
_TOKEN_KEYS: dict[str, str] = {
    "telegram_bot_token": "telegram",
    "slack_bot_token": "slack",
    "slack_app_token": "slack_app",
}


async def migrate_messaging_tokens(
    user_id: str | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Migrate messaging tokens from SettingsManager to auth DB.

    Args:
        user_id: Target user ID. If None, uses "local" (desktop default).
        dry_run: If True, log what would happen without modifying anything.

    Returns:
        (migrated_count, skipped_count)
    """
    if user_id is None:
        user_id = "local"

    migrated = 0
    skipped = 0

    # Load tokens from SettingsManager
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
    except Exception:
        logger.warning("SettingsManager not available; skipping messaging token migration")
        return 0, 0

    # Get auth DB
    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        if not db._initialized:
            await db.initialize()
    except Exception:
        logger.exception("Failed to get auth DB for messaging token migration")
        return 0, 0

    for settings_key, service_name in _TOKEN_KEYS.items():
        token = sm.get(settings_key, "")
        if not token or not isinstance(token, str) or token.strip() == "":
            skipped += 1
            continue

        # Check if already migrated
        existing = await db.user_credentials.get_credential(user_id, service_name)
        if existing:
            logger.debug(
                "Credential for service=%s already in DB, skipping",
                service_name,
            )
            skipped += 1
            continue

        if dry_run:
            logger.info(
                "[DRY RUN] Would migrate %s -> user_credentials(user=%s, service=%s)",
                settings_key,
                user_id,
                service_name,
            )
            migrated += 1
            continue

        # Write to DB
        try:
            await db.user_credentials.set_credential(user_id, service_name, token.strip())
            logger.info(
                "Migrated %s to user_credentials for user=%s service=%s",
                settings_key,
                user_id,
                service_name,
            )
            migrated += 1

            # Clear from settings.json (set to empty so it won't re-migrate)
            sm.set(settings_key, "", save_immediately=False)
        except Exception:
            logger.exception("Failed to migrate %s for user=%s", settings_key, user_id)

    # Save settings.json once if we made changes
    if migrated > 0 and not dry_run:
        sm.save()

    logger.info(
        "Messaging token migration complete: migrated=%d, skipped=%d",
        migrated,
        skipped,
    )
    return migrated, skipped


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Migrate messaging tokens to auth DB")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--user-id", default=None, help="Target user ID (default: 'local')")
    args = parser.parse_args()

    # Ensure project root is on sys.path
    project_root = str(Path(__file__).resolve().parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    migrated, skipped = asyncio.run(migrate_messaging_tokens(args.user_id, args.dry_run))
    print(f"Migrated: {migrated}, Skipped: {skipped}")  # noqa: T201


if __name__ == "__main__":
    main()
