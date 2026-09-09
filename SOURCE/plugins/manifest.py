"""
Plugin Manifest System

Parse and validate plugin.json manifest files.
Supports optional cryptographic signing for verified plugins.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class PluginManifestValidationError(ValueError):
    """Raised when plugin.json is structurally invalid."""


CRYPTO_AVAILABLE = False
_InvalidSignature: type[Exception] = Exception
_Ed25519PublicKey: type | None = None
_serialization = None

try:  # pragma: no cover - cryptography optional at runtime
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    CRYPTO_AVAILABLE = True
    _InvalidSignature = InvalidSignature
    _Ed25519PublicKey = Ed25519PublicKey
    _serialization = serialization
except ImportError:  # pragma: no cover - gracefully degrade without crypto
    pass


@dataclass
class PluginManifest:
    """Plugin manifest (plugin.json)"""

    name: str
    version: str | None = None
    api_version: str = "1.0"
    description: str = ""
    author: str | dict[str, Any] = ""
    permissions: list[str] = field(default_factory=list)
    entry_point: str = ""  # Python module path; Viola runtime extension
    dependencies: list[Any] = field(default_factory=list)
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: list[str] = field(default_factory=list)
    hooks: Any = None
    commands: Any = None
    agents: Any = None
    skills: Any = None
    output_styles: Any = None
    channels: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: Any = None
    lsp_servers: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    user_config: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None  # Cryptographic signature

    @classmethod
    def from_file(cls, manifest_path: Path) -> PluginManifest:
        """Load manifest from file"""
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        data = cls._normalize_manifest_keys(data)
        return cls(**data)

    @staticmethod
    def _normalize_manifest_keys(data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Claude camelCase aliases into Python field names."""
        normalized = dict(data)
        aliases = {
            "apiVersion": "api_version",
            "outputStyles": "output_styles",
            "mcpServers": "mcp_servers",
            "lspServers": "lsp_servers",
            "userConfig": "user_config",
        }
        for source, target in aliases.items():
            if source in normalized:
                if target not in normalized:
                    normalized[target] = normalized[source]
                del normalized[source]
        return normalized

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        data: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "description": self.description,
            "author": self.author,
            "permissions": self.permissions,
            "entry_point": self.entry_point,
            "dependencies": self.dependencies,
            "signature": self.signature,
        }
        optional_fields = {
            "homepage": self.homepage,
            "repository": self.repository,
            "license": self.license,
            "keywords": self.keywords,
            "hooks": self.hooks,
            "commands": self.commands,
            "agents": self.agents,
            "skills": self.skills,
            "outputStyles": self.output_styles,
            "channels": self.channels,
            "mcpServers": self.mcp_servers,
            "lspServers": self.lsp_servers,
            "settings": self.settings,
            "userConfig": self.user_config,
        }
        for key, value in optional_fields.items():
            if value not in (None, "", [], {}):
                data[key] = value
        return data

    def has_claude_components(self) -> bool:
        """Return True when the manifest declares Claude-style components."""
        return any(
            value not in (None, "", [], {})
            for value in (
                self.hooks,
                self.commands,
                self.agents,
                self.skills,
                self.output_styles,
                self.channels,
                self.mcp_servers,
                self.lsp_servers,
                self.settings,
                self.user_config,
            )
        )

    def validate(self) -> bool:
        """Validate manifest."""
        self._require_non_empty_string(self.name, "name")
        self._require_non_empty_string(self.api_version, "api_version")
        if self.entry_point and not isinstance(self.entry_point, str):
            self._invalid("entry_point must be a string")
        if not self.entry_point and not self.has_claude_components():
            self._invalid("entry_point is required for Viola runtime plugins")

        self._validate_string_list(self.permissions, "permissions")
        self._validate_dependency_list(self.dependencies)
        self._validate_string_list(self.keywords, "keywords")
        self._validate_channels(self.channels)
        self._validate_mapping(self.settings, "settings")
        self._validate_user_config(self.user_config)
        for field_name in ("hooks", "commands", "agents", "skills", "output_styles", "mcp_servers", "lsp_servers"):
            self._validate_component_field(field_name, getattr(self, field_name))

        parts = self.api_version.split(".")
        if len(parts) != 2:
            self._invalid("api_version must be in format X.Y")
        if not all(part.isdigit() for part in parts):
            self._invalid("api_version must be numeric")

        logger.info("✅ Manifest validated: %s v%s", self.name, self.version)
        return True

    @staticmethod
    def _invalid(message: str) -> None:
        raise PluginManifestValidationError(message)

    @classmethod
    def _require_non_empty_string(cls, value: Any, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            cls._invalid("%s is required" % field_name)

    @classmethod
    def _validate_string_list(cls, value: Any, field_name: str) -> None:
        if not isinstance(value, list):
            cls._invalid("%s must be a list" % field_name)
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                cls._invalid("%s[%d] must be a non-empty string" % (field_name, index))

    @classmethod
    def _validate_dependency_list(cls, value: Any) -> None:
        if not isinstance(value, list):
            cls._invalid("dependencies must be a list")
        for index, item in enumerate(value):
            if isinstance(item, str):
                if not item.strip():
                    cls._invalid("dependencies[%d] must be a non-empty string" % index)
                continue
            if isinstance(item, dict):
                cls._validate_json_compatible(item, "dependencies[%d]" % index)
                continue
            cls._invalid("dependencies[%d] must be a string or object" % index)

    @classmethod
    def _validate_channels(cls, value: Any) -> None:
        if not isinstance(value, list):
            cls._invalid("channels must be a list")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                cls._invalid("channels[%d] must be an object" % index)
            cls._validate_json_compatible(item, "channels[%d]" % index)

    @classmethod
    def _validate_mapping(cls, value: Any, field_name: str) -> None:
        if not isinstance(value, dict):
            cls._invalid("%s must be a dict" % field_name)
        cls._validate_json_compatible(value, field_name)

    @classmethod
    def _validate_user_config(cls, value: Any) -> None:
        if not isinstance(value, dict):
            cls._invalid("user_config must be a dict")
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                cls._invalid("user_config keys must be non-empty strings")
            if not isinstance(item, dict):
                cls._invalid("user_config.%s must be an object" % key)
            cls._validate_json_compatible(item, "user_config.%s" % key)

    @classmethod
    def _validate_component_field(cls, field_name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if not value.strip():
                cls._invalid("%s must not be empty" % field_name)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, (str, dict)):
                    cls._invalid("%s[%d] must be a string or object" % (field_name, index))
                if isinstance(item, str) and not item.strip():
                    cls._invalid("%s[%d] must not be empty" % (field_name, index))
                if isinstance(item, dict):
                    cls._validate_json_compatible(item, "%s[%d]" % (field_name, index))
            return
        if isinstance(value, dict):
            cls._validate_json_compatible(value, field_name)
            return
        cls._invalid("%s must be a string, list, or dict" % field_name)

    @classmethod
    def _validate_json_compatible(cls, value: Any, path: str) -> None:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                cls._validate_json_compatible(item, "%s[%d]" % (path, index))
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip():
                    cls._invalid("%s keys must be non-empty strings" % path)
                cls._validate_json_compatible(item, "%s.%s" % (path, key))
            return
        cls._invalid("%s must be JSON-compatible" % path)

    def verify_signature(self, public_key_pem: str) -> bool:
        """
        Verify cryptographic signature (optional)

        Args:
            public_key_pem: Public key in PEM format

        Returns:
            True if signature is valid
        """
        if not self.signature:
            logger.warning("No signature to verify for %s", self.name)
            return False

        if not CRYPTO_AVAILABLE or _serialization is None or _Ed25519PublicKey is None:
            logger.warning("cryptography library not available - cannot verify plugin signatures")
            return False

        try:
            # Import Ed25519PublicKey directly for proper type checking
            from cryptography.hazmat.primitives import serialization as serial_module
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey as Ed25519PubKeyType,
            )

            if isinstance(public_key_pem, str):
                public_key_bytes = public_key_pem.encode("utf-8")
            else:
                public_key_bytes = public_key_pem

            public_key = serial_module.load_pem_public_key(public_key_bytes)
            if not isinstance(public_key, Ed25519PubKeyType):
                logger.error(
                    "Unsupported public key type for %s: %s",
                    self.name,
                    type(public_key).__name__,
                )
                return False

            signature_bytes = base64.b64decode(self.signature)

            manifest_dict = self.to_dict().copy()
            manifest_dict.pop("signature", None)
            canonical_json = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")

            public_key.verify(signature_bytes, canonical_json)
        except (ValueError, TypeError) as exc:
            logger.error("Invalid signature payload for %s: %s", self.name, exc)
            return False
        except _InvalidSignature:
            logger.warning("Signature verification failed for %s", self.name)
            return False
        except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive logging
            logger.error("Unexpected error verifying signature for %s: %s", self.name, exc)
            return False

        logger.info("✅ Signature verified for %s", self.name)
        return True
