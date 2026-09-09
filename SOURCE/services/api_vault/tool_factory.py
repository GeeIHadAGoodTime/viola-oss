"""Tool Factory — constructs authenticated API calls at runtime.

The factory is the ONLY component that touches both the vault (credentials)
and the catalog (API metadata). It produces MCP-compatible tool definitions
and executes authenticated API requests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

import httpx

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from core.secrets_mask import mask_secrets_in_text
from core.url_validation import validate_external_url
from services.api_vault.vault import scrub_secrets

logger = get_logger(__name__)

_SENSITIVE_RESPONSE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "passwd",
    "bearer",
    "x_api_key",
)


def _is_sensitive_response_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_RESPONSE_KEY_PARTS)


def _redact_api_response_text(text: str, vault: object, *, user_id: str) -> str:
    redacted = scrub_secrets(text, vault, user_id=user_id)
    return mask_secrets_in_text(redacted)


def _redact_api_response_payload(data: Any, vault: object, *, user_id: str) -> Any:
    if isinstance(data, Mapping):
        return {
            key: (
                "[REDACTED]"
                if _is_sensitive_response_key(key)
                else _redact_api_response_payload(value, vault, user_id=user_id)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_redact_api_response_payload(value, vault, user_id=user_id) for value in data]
    if isinstance(data, tuple):
        return tuple(_redact_api_response_payload(value, vault, user_id=user_id) for value in data)
    if isinstance(data, str):
        return _redact_api_response_text(data, vault, user_id=user_id)
    return data


def _validate_endpoint(endpoint: str) -> str:
    """Ensure *endpoint* is a safe relative path (no URL override tricks).

    Raises ``ValueError`` if the endpoint contains ``://``, ``@``,
    protocol-relative ``//`` prefix, or path-traversal segments.
    Returns a normalised path that starts with ``/``.
    """
    decoded = endpoint.replace("\\", "/")
    for _ in range(3):
        next_decoded = unquote(decoded).replace("\\", "/")
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if "\x00" in decoded or any(ord(char) < 32 for char in decoded):
        raise ValueError("Endpoint must not contain control characters")
    if "://" in decoded:
        raise ValueError("Endpoint must be a relative path, not a full URL")
    if "@" in decoded:
        raise ValueError("Endpoint must not contain @")
    if decoded.startswith("//"):
        raise ValueError("Endpoint must not be protocol-relative")
    if ".." in decoded.split("/"):
        raise ValueError("Endpoint must not contain path traversal")
    return decoded if decoded.startswith("/") else "/" + decoded


class ToolFactory:
    """Constructs authenticated API calls from vault + catalog data.

    This is the bridge between the credential vault and the API catalog.
    It creates MCP-compatible tool schemas and executes authenticated requests.
    """

    def __init__(self) -> None:
        self._vault = None
        self._catalog = None

    @property
    def vault(self):
        """Lazy-loaded vault to avoid circular imports."""
        if self._vault is None:
            from services.api_vault.vault import get_credential_vault

            self._vault = get_credential_vault()
        return self._vault

    @property
    def catalog(self):
        """Lazy-loaded catalog to avoid circular imports."""
        if self._catalog is None:
            from services.api_vault.catalog import get_api_catalog

            self._catalog = get_api_catalog()
        return self._catalog

    def create_tool_definition(self, service_name: str, *, user_id: str) -> dict[str, Any] | None:
        """Create an MCP-compatible tool schema for a registered API.

        Args:
            service_name: The service to create a tool for

        Returns:
            MCP tool schema dict, or None if service not found
        """
        entry = self.catalog.get_entry(service_name)
        if entry is None:
            return None

        has_cred = self.vault.has_credential(service_name, user_id=user_id)

        return {
            "name": "api_%s" % service_name,
            "description": "%s (dynamic API). %s"
            % (
                entry.description,
                "Ready." if has_cred else "Needs API key.",
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "endpoint": {
                        "type": "string",
                        "description": "API endpoint path (e.g., /v1/pages)",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "default": "GET",
                        "description": "HTTP method",
                    },
                    "params": {
                        "type": "object",
                        "description": "Query parameters (for GET) or JSON body (for POST/PUT/PATCH)",
                        "default": {},
                    },
                    "headers": {
                        "type": "object",
                        "description": "Additional HTTP headers",
                        "default": {},
                    },
                },
                "required": ["endpoint"],
            },
            "_meta": {
                "service_name": service_name,
                "dynamic": True,
                "has_credential": has_cred,
                "capabilities": entry.capabilities,
            },
        }

    def get_available_dynamic_tools(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return all tool definitions from the catalog.

        Only returns tools for services that have stored credentials.
        """
        tools = []
        for entry_dict in self.catalog.get_catalog():
            service_name = entry_dict["service_name"]
            if self.vault.has_credential(service_name, user_id=user_id):
                tool_def = self.create_tool_definition(service_name, user_id=user_id)
                if tool_def:
                    tools.append(tool_def)
        return tools

    async def execute_api_call(
        self,
        service_name: str,
        endpoint: str,
        user_id: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated API call.

        Reads the key from the vault, constructs the request with proper
        auth headers, makes the call, and returns the result.

        Args:
            service_name: The API service to call
            endpoint: API endpoint path
            method: HTTP method
            params: Query params or request body
            headers: Additional headers

        Returns:
            Dict with 'ok', 'status_code', 'data', 'error' fields
        """
        if not user_id:
            return {
                "ok": False,
                "error": "user_id is required for dynamic API credentials.",
                "error_category": "EXPECTED_AUTH",
            }
        api_key = self.vault.get_credential(service_name, user_id=user_id)
        if api_key is None:
            return {
                "ok": False,
                "error": "No API key stored for '%s'. Provide one to enable this API." % service_name,
                "error_category": "TIER_GATED",
            }

        entry = self.catalog.get_entry(service_name)
        if entry is None:
            return {
                "ok": False,
                "error": "Service '%s' not found in API catalog." % service_name,
                "error_category": "UNEXPECTED_CONFIG",
            }

        # Build URL — validate endpoint is a safe relative path
        try:
            endpoint = _validate_endpoint(endpoint)
            base = entry.base_url.rstrip("/")
            url = validate_external_url(base + endpoint)
        except ValueError as exc:
            logger.warning(
                "Blocked unsafe dynamic API URL for service %s: %s",
                service_name,
                exc,
            )
            self.catalog.update_health(
                service_name,
                "unhealthy",
                "Blocked by outbound safety policy",
            )
            return {
                "ok": False,
                "error": "API endpoint is not allowed by Viola's outbound safety policy.",
                "error_category": "EXPECTED_SECURITY",
            }

        # Build auth headers
        req_headers = dict(headers or {})
        if entry.auth_type in ("bearer_token", "api_key"):
            if entry.header_prefix:
                req_headers[entry.header_name] = "%s %s" % (
                    entry.header_prefix,
                    api_key,
                )
            else:
                req_headers[entry.header_name] = api_key
        elif entry.auth_type == "header":
            req_headers[entry.header_name] = api_key

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
                if method.upper() == "GET":
                    response = await client.get(url, params=params, headers=req_headers)
                elif method.upper() in ("POST", "PUT", "PATCH"):
                    response = await client.request(
                        method.upper(),
                        url,
                        json=params,
                        headers=req_headers,
                    )
                elif method.upper() == "DELETE":
                    response = await client.delete(url, headers=req_headers)
                else:
                    return {
                        "ok": False,
                        "error": "Unsupported HTTP method: %s" % method,
                    }

            # Update health and usage
            if response.status_code < 400:
                self.catalog.update_health(service_name, "healthy")
                self.catalog.increment_usage(service_name)
            elif response.status_code == 401:
                self.catalog.update_health(service_name, "unhealthy", "Authentication failed (401)")
                return {
                    "ok": False,
                    "status_code": 401,
                    "error": "API key for '%s' was rejected (401). The key may be expired or invalid." % service_name,
                    "error_category": "EXPECTED_AUTH",
                }
            elif response.status_code == 429:
                self.catalog.update_health(service_name, "degraded", "Rate limited (429)")
                return {
                    "ok": False,
                    "status_code": 429,
                    "error": "Rate limited by %s API. Try again in a moment." % service_name,
                    "error_category": "EXPECTED_RATE_LIMIT",
                    "retryable": True,
                }
            elif response.status_code >= 500:
                self.catalog.update_health(
                    service_name,
                    "unhealthy",
                    "Server error (%d)" % response.status_code,
                )

            # Parse response
            try:
                data = response.json()
            except Exception:
                data = response.text
            data = _redact_api_response_payload(data, self.vault, user_id=user_id)

            return {
                "ok": response.status_code < 400,
                "status_code": response.status_code,
                "data": data,
            }

        except httpx.TimeoutException:
            self.catalog.update_health(service_name, "degraded", "Timeout")
            return {
                "ok": False,
                "error": "%s API request timed out." % service_name,
                "error_category": "EXPECTED_TIMEOUT",
                "retryable": True,
            }
        except httpx.ConnectError:
            self.catalog.update_health(service_name, "unhealthy", "Connection failed")
            return {
                "ok": False,
                "error": "Could not connect to %s API." % service_name,
                "error_category": "EXPECTED_NETWORK",
                "retryable": True,
            }
        except Exception as e:
            logger.exception("API call to %s failed", service_name)
            self.catalog.update_health(service_name, "unhealthy", str(e))
            return {
                "ok": False,
                "error": "%s API call failed: %s" % (service_name, type(e).__name__),
                "error_category": "UNEXPECTED_BUG",
            }


# Singleton
_factory: ToolFactory | None = None


def get_tool_factory() -> ToolFactory:
    """Get or create the global tool factory singleton."""
    global _factory
    if _factory is None:
        _factory = ToolFactory()
    return _factory
