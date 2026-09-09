"""
Fail-Fast Validation Utilities
==============================

Utilities to make code "fix at first glance" by validating early,
failing clearly, and providing actionable error messages.

Usage:
    from utils.failfast import validate, assert_type, check_dependency

    # Validate function inputs
    def play_music(query: str, volume: int = 50):
        validate(query, str, non_empty=True, name="query")
        validate(volume, int, min=0, max=100, name="volume")
        # ... rest of function

    # Assert types in critical paths
    result = api_call()
    assert_type(result, dict, name="api_response")

    # Check dependencies before use
    check_dependency("vlc", "python-vlc", "music/player/")
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Sized
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import TypedDict, TypeVar, cast

from core.logging_config import get_logger

logger = get_logger(__name__)

# Type variable for generic validation
T = TypeVar("T")


class ConfigSchemaSpec(TypedDict, total=False):
    type: type[object]
    required: bool
    default: object
    min: int | float
    max: int | float
    choices: list[object]
    validator: Callable[[object], tuple[bool, str]]


class ErrorCode(str, Enum):
    """Error codes for tracking and debugging."""

    VOLUME_INVALID = "ERR_VOLUME_INVALID"
    QUERY_EMPTY = "ERR_QUERY_EMPTY"
    API_KEY_MISSING = "ERR_API_KEY_MISSING"  # pragma: allowlist secret
    TYPE_MISMATCH = "ERR_TYPE_MISMATCH"
    VALUE_OUT_OF_RANGE = "ERR_VALUE_OUT_OF_RANGE"
    INVALID_CHOICE = "ERR_INVALID_CHOICE"
    EMPTY_VALUE = "ERR_EMPTY_VALUE"
    DEPENDENCY_MISSING = "ERR_DEPENDENCY_MISSING"
    CONFIG_INVALID = "ERR_CONFIG_INVALID"
    CUSTOM_VALIDATION_FAILED = "ERR_CUSTOM_VALIDATION_FAILED"


class BootValidationError(Exception):
    """Raised when bootstrap/startup validation fails.

    Raised when bootstrap/startup validation fails.
    """

    def __init__(self, message: str, error_code: ErrorCode | None = None):
        super().__init__(message)
        self.error_code = error_code


# Backward compatibility alias - deprecated, use BootValidationError
ValidationError = BootValidationError


class DependencyError(Exception):
    """Raised when required dependency is missing. Contains installation instructions."""

    pass


def validate(
    value: object,
    expected_type: type[T],
    *,
    name: str = "value",
    non_empty: bool = False,
    min: int | float | None = None,
    max: int | float | None = None,
    choices: list[object] | None = None,
    custom_validator: Callable[[T], tuple[bool, str]] | None = None,
    error_context: dict[str, object] | None = None,
    error_code: ErrorCode | None = None,
    example: str | None = None,
) -> T:
    """
    Validate a value with comprehensive error reporting.

    Args:
        value: Value to validate
        expected_type: Expected type (e.g., str, int, dict)
        name: Name of parameter (for error messages)
        non_empty: If True, reject empty strings/collections
        min: Minimum value (for numeric types)
        max: Maximum value (for numeric types)
        choices: Allowed values (for any type)
        custom_validator: Function(value) -> (is_valid, error_msg)
        error_context: Additional context to include in error

    Returns:
        Validated value (cast to expected_type if needed)

    Raises:
        ValidationError: If validation fails (with actionable message)

    Example:
        >>> validate("hello", str, non_empty=True, name="query")
        'hello'
        >>> validate(150, int, min=0, max=100, name="volume")
        ValidationError: volume must be 0-100, got 150
    """
    # Get calling context for better error messages
    frame = inspect.currentframe()
    caller_frame = frame.f_back if frame else None
    caller_info = ""
    if caller_frame:
        filename = caller_frame.f_code.co_filename
        lineno = caller_frame.f_lineno
        func_name = caller_frame.f_code.co_name
        caller_info = f"\n  Location: {Path(filename).name}:{lineno} in {func_name}()"

    # Type validation
    if not isinstance(value, expected_type):
        error_code_to_use = error_code or ErrorCode.TYPE_MISMATCH
        error_msg = (
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}"
            f"{caller_info}"
            f"\n  Value: {repr(value)[:100]}"
        )

        # Suggest fix
        if isinstance(value, str) and expected_type == int:
            error_msg += f"\n  Fix: Convert to int, e.g., int({value!r})"
        elif isinstance(value, int) and expected_type == str:
            error_msg += f"\n  Fix: Convert to str, e.g., str({value})"
        else:
            error_msg += f"\n  Fix: Pass {expected_type.__name__} instead of {type(value).__name__}"

        if example:
            error_msg += f"\n  Example: {example}"

        if error_context:
            error_msg += f"\n  Context: {error_context}"

        raise ValidationError(error_msg, error_code=error_code_to_use)

    typed_value = value

    # Non-empty validation
    if non_empty:
        error_code_to_use = error_code or ErrorCode.EMPTY_VALUE
        if isinstance(typed_value, str) and not typed_value.strip():
            error_msg = f"{name} cannot be empty or whitespace{caller_info}\n  Fix: Pass a non-empty string"
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)
        elif isinstance(typed_value, Sized) and len(typed_value) == 0:
            error_msg = f"{name} cannot be empty{caller_info}\n  Fix: Pass a non-empty {expected_type.__name__}"
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)

    # Range validation (for numeric types)
    if min is not None or max is not None:
        error_code_to_use = error_code or ErrorCode.VALUE_OUT_OF_RANGE
        if not isinstance(typed_value, (int, float)):
            raise ValidationError(
                f"{name} must be numeric for min/max validation, got {type(value).__name__}",
                error_code=error_code_to_use,
            )

        if min is not None and typed_value < min:
            range_str = f"{min}-{max}" if max is not None else f">= {min}"
            error_msg = (
                f"{name} must be {range_str}, got {typed_value}"
                f"{caller_info}"
                f"\n  Fix: Pass a value between {min} and {max if max is not None else 'infinity'}"
            )
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)

        if max is not None and typed_value > max:
            range_str = f"{min}-{max}" if min is not None else f"<= {max}"
            error_msg = (
                f"{name} must be {range_str}, got {typed_value}"
                f"{caller_info}"
                f"\n  Fix: Pass a value between {min if min is not None else '0'} and {max}"
            )
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)

    # Choices validation
    if choices is not None:
        error_code_to_use = error_code or ErrorCode.INVALID_CHOICE
        if typed_value not in choices:
            error_msg = (
                f"{name} must be one of {choices}, got {typed_value!r}"
                f"{caller_info}"
                f"\n  Fix: Pass one of the allowed values: {choices}"
            )
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)

    # Custom validator
    if custom_validator:
        error_code_to_use = error_code or ErrorCode.CUSTOM_VALIDATION_FAILED
        is_valid, custom_msg = custom_validator(typed_value)
        if not is_valid:
            error_msg = f"{name} failed custom validation: {custom_msg}{caller_info}"
            if example:
                error_msg += f"\n  Example: {example}"
            raise ValidationError(error_msg, error_code=error_code_to_use)

    return typed_value


def assert_type(value: object, expected_type: type[T], *, name: str = "value") -> T:
    """
    Assert that value is of expected type. Use in critical paths.

    Similar to validate() but simpler - just type checking.
    Use this for runtime type assertions in critical code paths.

    Args:
        value: Value to check
        expected_type: Expected type
        name: Name for error message

    Returns:
        Value (if type matches)

    Raises:
        ValidationError: If type doesn't match

    Example:
        >>> result = api_call()
        >>> assert_type(result, dict, name="api_response")
        {...}
    """
    if not isinstance(value, expected_type):
        frame = inspect.currentframe()
        caller_frame = frame.f_back if frame else None
        caller_info = ""
        if caller_frame:
            filename = caller_frame.f_code.co_filename
            lineno = caller_frame.f_lineno
            caller_info = f" at {Path(filename).name}:{lineno}"

        raise ValidationError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}{caller_info}\n"
            f"  Value: {repr(value)[:100]}"
        )

    return value


def check_dependency(
    module_name: str,
    package_name: str | None = None,
    context_file: str | None = None,
    min_version: str | None = None,
) -> ModuleType:
    """
    Check if a dependency is available. Raise clear error if missing.

    Args:
        module_name: Python module name to import (e.g., "vlc")
        package_name: Package name for pip (e.g., "python-vlc")
        context_file: File where dependency is used (for error message)
        min_version: Minimum version required

    Returns:
        Imported module

    Raises:
        DependencyError: If dependency is missing (with installation instructions)

    Example:
        >>> vlc = check_dependency("vlc", "python-vlc", "music/player/")
        <module 'vlc'>
    """
    package_name = package_name or module_name

    try:
        module = importlib.import_module(module_name)

        # Check version if specified
        if min_version and hasattr(module, "__version__"):
            from packaging import version

            if version.parse(module.__version__) < version.parse(min_version):
                raise DependencyError(
                    f"{package_name} version {module.__version__} is too old\n"
                    f"  Required: >= {min_version}\n"
                    f"  Fix: pip install --upgrade {package_name}"
                )

        return module

    except ImportError as e:
        error_msg = f"Missing dependency: {package_name}\n  Module: {module_name}\n  Error: {e}"

        if context_file:
            error_msg += f"\n  Used in: {context_file}"

        error_msg += (
            f"\n  Fix: pip install {package_name}\n  Or install all deps: pip install -r requirements_desktop.txt"
        )

        raise DependencyError(error_msg) from e


def validate_config(
    config_dict: dict[str, object],
    schema: dict[str, ConfigSchemaSpec],
    config_source: str = "configuration",
) -> dict[str, object]:
    """
    Validate configuration dictionary against schema.

    Args:
        config_dict: Configuration to validate
        schema: Schema definition
            {
                "key": {
                    "type": type,
                    "required": bool,
                    "default": value,
                    "min": value,
                    "max": value,
                    "choices": list,
                    "validator": callable
                }
            }
        config_source: Source name (for error messages)

    Returns:
        Validated config dict

    Raises:
        ValidationError: If validation fails

    Example:
        >>> schema = {
        ...     "api_port": {"type": int, "required": True, "min": 1, "max": 65535},
        ...     "log_level": {"type": str, "required": False, "default": "INFO", "choices": ["DEBUG", "INFO", "WARNING"]}
        ... }
        >>> validate_config({"api_port": 8756}, schema)
        {"api_port": 8756, "log_level": "INFO"}
    """
    validated = {}
    errors = []

    # Check required fields
    for key, spec in schema.items():
        if spec.get("required", False) and key not in config_dict:
            errors.append(f"Missing required field: {key}\n  Fix: Add {key} to {config_source}")

    # Validate all provided fields
    for key, value in config_dict.items():
        if key not in schema:
            errors.append(f"Unknown field: {key}\n  Fix: Remove {key} from {config_source} or add to schema")
            continue

        spec = schema[key]
        expected_type = spec.get("type")

        # Type validation
        if expected_type is not None:
            try:
                validate(value, expected_type, name=key)
            except ValidationError as e:
                errors.append(f"{key}: {e}")
                continue

        # Range validation
        if "min" in spec or "max" in spec:
            try:
                validate(
                    value,
                    expected_type or type(value),
                    min=spec.get("min"),
                    max=spec.get("max"),
                    name=key,
                )
            except ValidationError as e:
                errors.append(f"{key}: {e}")
                continue

        # Choices validation
        if "choices" in spec:
            choices = spec.get("choices")
            if choices is not None:
                try:
                    validate(
                        value,
                        expected_type or type(value),
                        choices=choices,
                        name=key,
                    )
                except ValidationError as e:
                    errors.append(f"{key}: {e}")
                    continue

        # Custom validator
        validator = spec.get("validator")
        if validator is not None:
            is_valid, msg = validator(value)
            if not is_valid:
                errors.append(f"{key}: {msg}")
                continue

        validated[key] = value

    # Apply defaults
    for key, spec in schema.items():
        if key not in validated and "default" in spec:
            validated[key] = spec["default"]

    if errors:
        error_msg = f"{config_source} validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValidationError(error_msg)

    return validated


def enrich_error(
    error: Exception,
    context: dict[str, object] | None = None,
    auto_capture: bool = True,
) -> Exception:
    """
    Enrich an exception with additional context, including automatic capture of
    function parameters from the caller.

    Args:
        error: Original exception
        context: Additional context dict (merged with auto-captured context)
        auto_capture: If True, automatically capture caller's function parameters

    Returns:
        New exception with enriched message

    Example:
        >>> try:
        ...     process_command("invalid")
        ... except ValueError as e:
        ...     raise enrich_error(e, {"command": "invalid", "timestamp": time.time()})
    """
    frame = inspect.currentframe()
    caller_frame = frame.f_back if frame else None

    caller_info = ""
    auto_context = {}

    if caller_frame:
        filename = caller_frame.f_code.co_filename
        lineno = caller_frame.f_lineno
        func_name = caller_frame.f_code.co_name
        caller_info = f"\n  Location: {Path(filename).name}:{lineno} in {func_name}()"

        # Auto-capture function parameters if enabled
        if auto_capture:
            try:
                # Get function parameters
                params = caller_frame.f_locals.copy()

                # Remove internal variables, keep only function arguments
                # (This captures self, args, kwargs, and function parameters)
                for key, value in params.items():
                    # Limit value size for readability
                    if isinstance(value, str):
                        value_str = value[:200] + "..." if len(value) > 200 else value
                    elif hasattr(value, "__len__") and len(str(value)) > 200:
                        value_str = str(value)[:200] + "..."
                    else:
                        value_str = value

                    auto_context[f"param_{key}"] = value_str
            except Exception as e:
                # If capture fails, just continue without auto context
                logger.debug("Failed to capture parameter context: %s", e, exc_info=True)
                pass

    # Merge contexts (provided context overrides auto-captured)
    full_context = {**auto_context, **(context or {})}

    if full_context:
        context_str = "\n".join(f"  {k}: {repr(v)[:200]}" for k, v in full_context.items())
        enriched_msg = f"{error!s}{caller_info}\n  Context:\n{context_str}"
    else:
        enriched_msg = f"{error!s}{caller_info}"

    # Create new exception of same type with enriched message
    # Note: We can't use 'from error' in a return statement, so we create the exception
    # and the caller should raise it with 'from error' if they want chaining
    if isinstance(error, BootValidationError):
        return BootValidationError(enriched_msg, error_code=error.error_code)

    exc_type = type(error)
    return exc_type(enriched_msg)


# Convenience aliases
v = validate  # Short alias for validate()
at = assert_type  # Short alias for assert_type()
