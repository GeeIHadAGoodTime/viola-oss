"""
Auth Module Startup Validation.

This module provides validation functions that check for critical dependencies
BEFORE the auth routes are imported. This prevents silent failures that lead
to confusing 404 errors when auth routes don't get registered.

Usage:
    from auth.startup_validation import validate_auth_dependencies

    # Call early in app startup
    validate_auth_dependencies(fail_fast=True)

Why this exists:
    The auth module uses Pydantic's EmailStr (requires email-validator) and
    FastAPI's Form() (requires python-multipart). If either is missing:
    1. The auth module fails to import
    2. The exception is caught and logged as a warning
    3. Auth routes are NOT registered
    4. Users get {"detail":"Not Found"} with no clear explanation

    This module catches these issues early with clear error messages.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TypedDict

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DependencyCheck:
    """Result of a dependency check."""

    name: str
    package: str
    import_path: str
    required_for: str
    is_available: bool
    error: str | None = None
    install_command: str | None = None


def check_email_validator() -> DependencyCheck:
    """Check if email-validator is installed."""
    try:
        import email_validator

        # Verify it actually works
        email_validator.validate_email("test@example.com", check_deliverability=False)

        return DependencyCheck(
            name="email-validator",
            package="email-validator",
            import_path="email_validator",
            required_for="Pydantic EmailStr type in auth models",
            is_available=True,
            install_command="pip install email-validator>=2.0.0",
        )
    except ImportError as e:
        return DependencyCheck(
            name="email-validator",
            package="email-validator",
            import_path="email_validator",
            required_for="Pydantic EmailStr type in auth models",
            is_available=False,
            error=str(e),
            install_command="pip install email-validator>=2.0.0",
        )
    except Exception as e:
        return DependencyCheck(
            name="email-validator",
            package="email-validator",
            import_path="email_validator",
            required_for="Pydantic EmailStr type in auth models",
            is_available=False,
            error=f"Installed but not working: {e}",
            install_command="pip install --upgrade email-validator>=2.0.0",
        )


def check_python_multipart() -> DependencyCheck:
    """Check if python-multipart is installed."""
    try:
        from importlib import import_module

        import_module("multipart")

        return DependencyCheck(
            name="python-multipart",
            package="python-multipart",
            import_path="multipart",
            required_for="FastAPI Form() parameters in OAuth callbacks",
            is_available=True,
            install_command="pip install python-multipart",
        )
    except ImportError as e:
        return DependencyCheck(
            name="python-multipart",
            package="python-multipart",
            import_path="multipart",
            required_for="FastAPI Form() parameters in OAuth callbacks",
            is_available=False,
            error=str(e),
            install_command="pip install python-multipart",
        )


def check_bcrypt() -> DependencyCheck:
    """Check if bcrypt is installed (for password hashing)."""
    try:
        bcrypt = importlib.import_module("bcrypt")

        # Verify it works
        bcrypt.hashpw(b"test", bcrypt.gensalt())

        return DependencyCheck(
            name="bcrypt",
            package="bcrypt",
            import_path="bcrypt",
            required_for="Password hashing in auth system",
            is_available=True,
            install_command="pip install bcrypt",
        )
    except ImportError as e:
        return DependencyCheck(
            name="bcrypt",
            package="bcrypt",
            import_path="bcrypt",
            required_for="Password hashing in auth system",
            is_available=False,
            error=str(e),
            install_command="pip install bcrypt",
        )
    except Exception as e:
        return DependencyCheck(
            name="bcrypt",
            package="bcrypt",
            import_path="bcrypt",
            required_for="Password hashing in auth system",
            is_available=False,
            error=f"Installed but not working: {e}",
            install_command="pip install --upgrade bcrypt",
        )


def validate_auth_dependencies(
    fail_fast: bool = False,
    log_results: bool = True,
) -> tuple[bool, list[DependencyCheck]]:
    """
    Validate all auth module dependencies.

    Args:
        fail_fast: If True, raise exception on first missing dependency.
                   If False, check all and return results.
        log_results: If True, log the validation results.

    Returns:
        Tuple of (all_ok, list of DependencyCheck results)

    Raises:
        RuntimeError: If fail_fast=True and a dependency is missing.
    """
    checks = [
        check_email_validator(),
        check_python_multipart(),
        check_bcrypt(),
    ]

    missing = [c for c in checks if not c.is_available]
    all_ok = len(missing) == 0

    if log_results:
        if all_ok:
            logger.info("✅ All auth dependencies available")
        else:
            logger.error("❌ Missing auth dependencies:")
            for check in missing:
                logger.error(
                    "   - %s: %s",
                    check.name,
                    check.error or "Not installed",
                )
                logger.error("     Required for: %s", check.required_for)
                logger.error("     Install with: %s", check.install_command)

    if fail_fast and not all_ok:
        error_lines = [
            "Missing required dependencies for auth system:",
            "",
        ]
        for check in missing:
            error_lines.append(f"  - {check.name}: {check.error or 'Not installed'}")
            error_lines.append(f"    Required for: {check.required_for}")
            error_lines.append(f"    Install with: {check.install_command}")
            error_lines.append("")

        error_lines.append("Install all with:")
        error_lines.append("  pip install email-validator>=2.0.0 python-multipart bcrypt")

        raise RuntimeError("\n".join(error_lines))

    return all_ok, checks


def validate_auth_import() -> tuple[bool, str | None]:
    """
    Validate that auth module can be imported.

    Returns:
        Tuple of (success, error_message)
    """
    try:
        from auth.routes import auth_router

        route_count = len(auth_router.routes)
        logger.info("✅ Auth routes importable (%d routes)", route_count)
        return True, None

    except ImportError as e:
        error = f"Auth module import failed: {e}"
        logger.error("❌ %s", error)
        return False, error

    except Exception as e:
        error = f"Auth module import failed with unexpected error: {e}"
        logger.error("❌ %s", error)
        return False, error


class AuthDependencyStatus(TypedDict):
    name: str
    available: bool
    error: str | None
    required_for: str
    install_command: str | None


class AuthStatus(TypedDict):
    dependencies_ok: bool
    import_ok: bool
    overall_ok: bool
    dependencies: list[AuthDependencyStatus]
    import_error: str | None


def get_auth_status() -> AuthStatus:
    """
    Get comprehensive auth system status.

    Returns:
        Dict with status information for diagnostics.
    """
    all_ok, checks = validate_auth_dependencies(fail_fast=False, log_results=False)
    import_ok, import_error = validate_auth_import()

    return {
        "dependencies_ok": all_ok,
        "import_ok": import_ok,
        "overall_ok": all_ok and import_ok,
        "dependencies": [
            {
                "name": c.name,
                "available": c.is_available,
                "error": c.error,
                "required_for": c.required_for,
                "install_command": c.install_command,
            }
            for c in checks
        ],
        "import_error": import_error,
    }


__all__ = [
    "DependencyCheck",
    "get_auth_status",
    "validate_auth_dependencies",
    "validate_auth_import",
]
