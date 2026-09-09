"""
YouTube Music provider error classification and handling.

This module contains error classification logic for YouTube Data API v3 errors,
distinguishing between app-level config issues, user account issues, quota issues, etc.
"""

from __future__ import annotations

from enum import Enum


class ProviderErrorKind(Enum):
    """Classification of provider error types for error handling."""

    API_AUTH = "api_auth"  # 401/403 with auth/config issues (app-level)
    API_QUOTA = "api_quota"  # 403 with quotaExceeded
    API_OTHER = "api_other"  # Other 4xx/5xx API errors
    USER_ACCOUNT = "user_account"  # Token revoked / invalid_grant / user-level issues
    NETWORK = "network"  # Network/transport errors
    UNKNOWN = "unknown"  # Unknown errors


def _classify_api_error(
    status_code: int,
    error_details: dict | None = None,
) -> ProviderErrorKind:
    """
    Classify YouTube Data API errors to distinguish app-level config issues from user account issues.

    This function inspects the actual error response payload from YouTube Data API v3 to determine
    the root cause. It differentiates between:
    - APP_CONFIG: Google Cloud project misconfiguration (API not enabled, wrong project, etc.)
    - QUOTA: API quota limits exceeded
    - USER_ACCOUNT: User OAuth token invalid/expired/revoked
    - OTHER_API: Transient errors, network issues, etc.

    Args:
        status_code: HTTP status code from API response
        error_details: Optional error JSON from API response (YouTube API error format)

    Returns:
        ProviderErrorKind classification

    Note:
        YouTube Data API v3 error format:
        {
            "error": {
                "code": 403,
                "message": "Access Not Configured",
                "errors": [{
                    "domain": "usageLimits",
                    "reason": "accessNotConfigured",
                    "message": "Access Not Configured. YouTube Data API v3 has not been used..."
                }]
            }
        }
    """
    # Extract error reason and domain from error_details if available
    error_reason = None
    error_domain = None

    if error_details and isinstance(error_details, dict):
        error_obj = error_details.get("error", {})
        if isinstance(error_obj, dict):
            errors_list = error_obj.get("errors", [])
            if errors_list and isinstance(errors_list, list) and len(errors_list) > 0:
                first_err = errors_list[0]
                if isinstance(first_err, dict):
                    error_reason = first_err.get("reason", "")
                    error_domain = first_err.get("domain", "")

    # Classify 403 errors based on error reason and domain
    if status_code == 403:
        # Quota-related 403 errors (check both reason and domain)
        # Domain "youtube.quota" is a strong indicator of quota issues
        if error_domain == "youtube.quota" or error_reason in (
            "quotaExceeded",
            "dailyLimitExceeded",
            "userRateLimitExceeded",
        ):
            return ProviderErrorKind.API_QUOTA

        # App-level configuration issues (API not enabled, wrong project, insufficient permissions)
        # These indicate the Google Cloud project needs configuration changes
        if error_reason in (
            "accessNotConfigured",  # YouTube Data API v3 not enabled on project
            "insufficientPermissions",  # API credentials lack required permissions
            "forbidden",  # Generic forbidden (usually config-related for 403)
            "projectNotLinked",  # OAuth client not linked to project
            "projectInvalid",  # Invalid project configuration
        ):
            return ProviderErrorKind.API_AUTH

        # If we have a 403 but reason is unknown/missing, default to APP_CONFIG
        # This is the most common case for 403 from YouTube Data API
        # (API not enabled, wrong project, etc.)
        return ProviderErrorKind.API_AUTH

    # Classify 401 errors (authentication failures)
    if status_code == 401:
        # Check if it's a user token issue vs app config issue
        if error_reason in (
            "invalid_grant",
            "invalid_token",
            "token_expired",
            "invalid_credentials",
        ):
            return ProviderErrorKind.USER_ACCOUNT

        # If we have a token but get 401, it's likely user-level (token expired/revoked)
        # Default to USER_ACCOUNT for 401
        return ProviderErrorKind.USER_ACCOUNT

    # Check error details for specific error codes (for other status codes)
    if error_reason:
        # App-level configuration issues (can occur with other status codes)
        if error_reason in (
            "accessNotConfigured",
            "insufficientPermissions",
            "forbidden",
        ):
            return ProviderErrorKind.API_AUTH

        # Quota issues (check both reason and domain)
        if error_domain == "youtube.quota" or error_reason in (
            "quotaExceeded",
            "dailyLimitExceeded",
            "userRateLimitExceeded",
        ):
            return ProviderErrorKind.API_QUOTA

        # User account issues
        if error_reason in (
            "invalid_grant",
            "invalid_token",
            "token_expired",
            "invalid_credentials",
        ):
            return ProviderErrorKind.USER_ACCOUNT

    # Classify by status code if no detailed error info
    if 400 <= status_code < 500:
        # For 4xx errors without specific reason, default to API_OTHER
        return ProviderErrorKind.API_OTHER
    elif status_code >= 500:
        # 5xx errors are transient server issues
        return ProviderErrorKind.API_OTHER

    return ProviderErrorKind.UNKNOWN
