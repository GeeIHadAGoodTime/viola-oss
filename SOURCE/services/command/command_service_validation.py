"""
Command Service Validation and Response Logic.

This module contains command validation and response handling logic
extracted from the main CommandService class to comply with code constraints.
"""

from __future__ import annotations

from typing import Any

from contracts.api_response import ResponseEnvelope
from core.constants import MAX_COMMAND_TEXT_LENGTH
from core.logging_config import get_logger

logger = get_logger(__name__)


class CommandValidationError(Exception):
    """Raised when command text validation fails."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CommandServiceValidator:
    """Handles command validation and normalization."""

    def normalize(self, raw: Any) -> str:
        """
        Normalize raw input to a clean command string.

        Args:
            raw: Raw input (string, bytes, etc.)

        Returns:
            Normalized command string

        Raises:
            CommandValidationError: If validation fails
        """

        if raw is None:
            raise CommandValidationError("Command text cannot be None")

        # Convert to string
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise CommandValidationError("Command text contains invalid UTF-8") from e
        else:
            text = str(raw)

        # Basic cleaning
        text = text.strip()

        if not text:
            raise CommandValidationError("Command text cannot be empty")

        # Length limits
        if len(text) > MAX_COMMAND_TEXT_LENGTH:
            raise CommandValidationError(f"Command text is too long (max {MAX_COMMAND_TEXT_LENGTH} characters)")

        # Validate command text using core validation
        try:
            from core.validation import validate_command_text

            is_valid, error_msg = validate_command_text(text)
            if not is_valid:
                logger.debug("Command validation failed: %s", error_msg)
                raise CommandValidationError(
                    "Your request contains text that couldn't be processed. Please try rephrasing."
                )
        except ImportError:
            # Fallback if validation not available
            pass

        return text


class CommandServiceResponseHandler:
    """Handles command response formatting and processing."""

    def __init__(self, service_instance):
        """
        Initialize response handler.

        Args:
            service_instance: The CommandService instance
        """
        self.service = service_instance

    def format_response(self, result: Any) -> ResponseEnvelope:
        """
        Format a command execution result into a proper response.

        Args:
            result: Raw command execution result

        Returns:
            Formatted response dictionary
        """
        try:
            # Handle different result types
            if hasattr(result, "to_envelope"):
                # CommandResult object
                return result.to_envelope()
            elif isinstance(result, dict):
                # Already a dict response
                from contracts.api_response import success_response

                return success_response(result)
            else:
                # Wrap in success response
                from contracts.api_response import success_response

                return success_response(result)

        except Exception as e:
            logger.exception("Failed to format response: %s", e)
            from contracts.api_response import failure_response

            return failure_response(
                code="response_formatting_failed",
                message=f"Response formatting failed: {e!s}",
            )

    def extract_intent_type(self, parsed: Any) -> str:
        """
        Extract intent type from parsed command result.

        Args:
            parsed: Parsed command result

        Returns:
            Intent type string
        """
        try:
            if hasattr(parsed, "intent") and hasattr(parsed.intent, "name"):
                return parsed.intent.name
            elif isinstance(parsed, dict) and "intent" in parsed:
                return parsed["intent"]
            else:
                return "unknown"
        except Exception:
            logger.debug("Failed to extract intent type", exc_info=True)
            return "unknown"

    def extract_params(self, parsed: Any) -> dict[str, Any]:
        """
        Extract parameters from parsed command result.

        Args:
            parsed: Parsed command result

        Returns:
            Parameters dictionary
        """
        try:
            if hasattr(parsed, "intent") and hasattr(parsed.intent, "args"):
                return dict(parsed.intent.args)
            elif isinstance(parsed, dict) and "params" in parsed:
                return parsed["params"]
            else:
                return {}
        except Exception:
            logger.debug("Failed to extract params", exc_info=True)
            return {}

    def coerce_queue_item(self, enqueued: Any) -> dict[str, Any] | None:
        """
        Coerce various queue item representations to a standard dict format.

        Args:
            enqueued: Queue item in various formats

        Returns:
            Standardized queue item dict, or None if coercion failed
        """
        try:
            if enqueued is None:
                return None

            # Already a dict
            if isinstance(enqueued, dict):
                return enqueued

            # QueueItem object
            if hasattr(enqueued, "id") and hasattr(enqueued, "title"):
                return {
                    "id": enqueued.id,
                    "title": enqueued.title,
                    "artist": getattr(enqueued, "artist", None),
                    "album": getattr(enqueued, "album", None),
                    "duration": getattr(enqueued, "duration", None),
                    "provider": getattr(enqueued, "provider", None),
                    "url": getattr(enqueued, "url", None),
                }

            # String (assume title)
            if isinstance(enqueued, str):
                return {
                    "id": f"item_{hash(enqueued)}",
                    "title": enqueued,
                    "artist": None,
                    "album": None,
                    "duration": None,
                    "provider": None,
                    "url": None,
                }

            logger.debug("Unable to coerce queue item of type: %s", type(enqueued))
            return None

        except Exception as e:
            logger.debug("Queue item coercion failed: %s", e)
            return None
