"""
OAuth Token Migration: Legacy (v0) to PBKDF2 (v1) Encryption.

This script migrates OAuth tokens from legacy SHA256-based encryption (key_version=0)
to PBKDF2-HMAC-SHA256 encryption with fixed salt (key_version=1).

Note: v1→v2 (per-user salt) migration happens transparently on read via
SQLiteOAuthTokenRepository.get_tokens(). This script only handles v0→v1.

Usage:
    python -m auth.migrations.migrate_oauth_tokens [--db-path PATH] [--dry-run]

The migration is idempotent: running it multiple times is safe.
Tokens already at key_version>=1 are skipped.

Requirements:
    - JWT_SECRET environment variable must be set
    - Database must be accessible

Example:
    # Dry run (no changes made)
    python -m auth.migrations.migrate_oauth_tokens --dry-run

    # Actual migration
    python -m auth.migrations.migrate_oauth_tokens
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger("viola.auth.migrations.oauth_tokens")


def migrate_oauth_tokens(
    db_path: Path,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """
    Migrate OAuth tokens from legacy to current encryption.

    Reads all tokens with key_version=0 (legacy), decrypts with legacy key,
    re-encrypts with current key, and updates key_version to 1.

    Args:
        db_path: Path to the SQLite auth database
        dry_run: If True, don't make changes, just report what would be done

    Returns:
        Tuple of (migrated_count, skipped_count, error_count)

    Raises:
        ValueError: If JWT_SECRET is not configured
        FileNotFoundError: If database file doesn't exist
    """
    from cryptography.fernet import Fernet, InvalidToken

    from auth.kdf import (
        KEY_VERSION_1,
        KEY_VERSION_LEGACY,
        derive_fernet_key,
        get_version_name,
    )
    from config.settings import settings

    # Validate JWT_SECRET
    if not settings.jwt_secret:
        raise ValueError(
            "JWT_SECRET environment variable is required for token migration. "
            "Set JWT_SECRET to the same value used when tokens were stored."
        )

    if not db_path.exists():
        raise FileNotFoundError("Database not found: %s" % db_path)

    logger.info("Starting OAuth token migration")
    logger.info("Database: %s", db_path)
    logger.info("Dry run: %s", dry_run)
    logger.info(
        "Migrating from %s to %s",
        get_version_name(KEY_VERSION_LEGACY),
        get_version_name(KEY_VERSION_1),
    )

    # Derive keys (v0→v1 only; v1→v2 per-user migration happens on read)
    legacy_key = derive_fernet_key(settings.jwt_secret, KEY_VERSION_LEGACY)
    current_key = derive_fernet_key(settings.jwt_secret, KEY_VERSION_1)

    legacy_fernet = Fernet(legacy_key)
    current_fernet = Fernet(current_key)

    # Connect to database
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # Manual transaction control
    conn.row_factory = sqlite3.Row

    try:
        # Check if key_version column exists
        cursor = conn.execute("PRAGMA table_info(oauth_tokens)")
        columns = {row[1] for row in cursor.fetchall()}

        if "key_version" not in columns:
            logger.warning(
                "key_version column not found in oauth_tokens table. " "Run the auth database initialization first."
            )
            return 0, 0, 0

        # Get all tokens with legacy key_version
        cursor = conn.execute("""
            SELECT id, user_id, provider, access_token_encrypted, refresh_token_encrypted
            FROM oauth_tokens
            WHERE key_version = 0 OR key_version IS NULL
            """)
        legacy_tokens = cursor.fetchall()

        total_count = len(legacy_tokens)
        logger.info("Found %d tokens with legacy encryption", total_count)

        if total_count == 0:
            logger.info("No tokens to migrate")
            return 0, 0, 0

        migrated_count = 0
        skipped_count = 0
        error_count = 0

        # Begin explicit transaction for atomicity
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
            logger.debug("Started migration transaction")

        try:
            for row in legacy_tokens:
                token_id = row["id"]
                user_id = row["user_id"]
                provider = row["provider"]
                access_encrypted = row["access_token_encrypted"]
                refresh_encrypted = row["refresh_token_encrypted"]

                logger.debug(
                    "Processing token %s (user=%s, provider=%s)",
                    token_id,
                    user_id,
                    provider,
                )

                try:
                    # Decrypt with legacy key
                    access_decrypted = None
                    refresh_decrypted = None

                    if access_encrypted:
                        try:
                            access_decrypted = legacy_fernet.decrypt(access_encrypted.encode()).decode()
                        except InvalidToken:
                            logger.warning(
                                "Failed to decrypt access token with legacy key (token=%s). "
                                "Token may already be encrypted with current key or is corrupted.",
                                token_id,
                            )
                            # Try with current key to check if already migrated
                            try:
                                current_fernet.decrypt(access_encrypted.encode())
                                logger.info(
                                    "Token %s already uses current encryption, skipping",
                                    token_id,
                                )
                                skipped_count += 1
                                continue
                            except InvalidToken:
                                error_count += 1
                                continue

                    if refresh_encrypted:
                        try:
                            refresh_decrypted = legacy_fernet.decrypt(refresh_encrypted.encode()).decode()
                        except InvalidToken:
                            logger.warning(
                                "Failed to decrypt refresh token with legacy key (token=%s). "
                                "Access token migrated, refresh token will be cleared.",
                                token_id,
                            )
                            # Continue with migration - access token is valid,
                            # refresh token will remain encrypted (unusable)
                            # User will need to re-authenticate for refresh

                    # Re-encrypt with current key
                    new_access_encrypted = None
                    new_refresh_encrypted = None

                    if access_decrypted:
                        new_access_encrypted = current_fernet.encrypt(access_decrypted.encode()).decode()

                    if refresh_decrypted:
                        new_refresh_encrypted = current_fernet.encrypt(refresh_decrypted.encode()).decode()

                    if dry_run:
                        logger.info("[DRY RUN] Would migrate token %s", token_id)
                        migrated_count += 1
                        continue

                    # Update database
                    now = datetime.now(UTC).isoformat()
                    conn.execute(
                        """
                        UPDATE oauth_tokens
                        SET access_token_encrypted = COALESCE(?, access_token_encrypted),
                            refresh_token_encrypted = COALESCE(?, refresh_token_encrypted),
                            key_version = 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            new_access_encrypted,
                            new_refresh_encrypted,
                            now,
                            token_id,
                        ),
                    )

                    migrated_count += 1
                    logger.debug("Migrated token %s", token_id)

                except Exception:
                    logger.exception("Error migrating token %s", token_id)
                    error_count += 1
                    continue

            # Commit transaction if not dry run
            if not dry_run:
                conn.execute("COMMIT")
                logger.info("Migration committed to database")

        except Exception:
            # Rollback on any unhandled error during migration
            if not dry_run:
                logger.warning("Rolling back migration transaction due to error")
                conn.execute("ROLLBACK")
            raise

        logger.info(
            "Migration complete: migrated=%d, skipped=%d, errors=%d",
            migrated_count,
            skipped_count,
            error_count,
        )

        return migrated_count, skipped_count, error_count

    finally:
        conn.close()


def main() -> int:
    """Main entry point for the migration script."""
    parser = argparse.ArgumentParser(description="Migrate OAuth tokens from legacy to PBKDF2 encryption")
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Path to auth database (default: <data_dir>/auth.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes",
    )

    args = parser.parse_args()

    try:
        # Get database path
        if args.db_path:
            db_path = args.db_path
        else:
            from config.settings import settings

            db_path = Path(settings.data_dir) / "auth.db"

        _migrated, _skipped, errors = migrate_oauth_tokens(db_path, args.dry_run)

        if errors > 0:
            return 1
        return 0

    except ValueError as e:
        logger.error("Configuration error: %s", e)
        return 1
    except FileNotFoundError as e:
        logger.error("File not found: %s", e)
        return 1
    except Exception:
        logger.exception("Migration failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
