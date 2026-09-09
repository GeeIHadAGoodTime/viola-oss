"""Plugin error tagged-union (F-056).

Claude exposes a discriminated `PluginError` union for parse errors,
validation errors, blocked marketplaces, missing paths, dependency
failures, MCP/LSP failures, cache misses, and generic errors. Viola
mirrors that shape so SDK/UI clients can distinguish retryable network
or cache failures from authoring errors, blocked policy, dependency
closure, or runtime crashes.

Human-readable text always lives on ``message``; ``kind`` is the
machine-readable discriminator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Error kinds (Claude TS reference: types/plugin.ts:101-288)
# ---------------------------------------------------------------------------

PARSE_ERROR = "parse-error"
VALIDATION_ERROR = "validation-error"
NOT_FOUND = "not-found"
LOAD_ERROR = "load-error"
RELOAD_ERROR = "reload-error"
INSTALL_ERROR = "install-error"
PERMISSION_DENIED = "permission-denied"
DEPENDENCY_UNSATISFIED = "dependency-unsatisfied"
UNSUPPORTED_COMPONENT = "unsupported-component"
MCP_FAILURE = "mcp-failure"
LSP_FAILURE = "lsp-failure"
MARKETPLACE_BLOCKED = "marketplace-blocked"
MARKETPLACE_FETCH_ERROR = "marketplace-fetch-error"
MARKETPLACE_NOT_IMPLEMENTED = "marketplace-not-implemented"
CACHE_MISS = "cache-miss"
INCOMPATIBLE_API = "incompatible-api"
SIGNATURE_INVALID = "signature-invalid"


# Kinds that may legitimately succeed on retry (transient).
RETRYABLE_KINDS: frozenset[str] = frozenset(
    {
        MCP_FAILURE,
        LSP_FAILURE,
        MARKETPLACE_FETCH_ERROR,
        CACHE_MISS,
    }
)


@dataclass(frozen=True)
class _PluginErrorData:
    """Tagged plugin error payload.

    Attributes:
        kind: Machine-readable discriminator (see ``*_ERROR`` constants).
        message: Human-readable description.
        plugin_name: Optional plugin id; ``None`` for top-level errors
            (e.g. an unparseable manifest in discovery).
        details: Per-kind extra payload (e.g. ``{"missing": ["pluginA"]}``
            for ``dependency-unsatisfied``).
    """

    kind: str
    message: str
    plugin_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("PluginError.kind is required")
        if not self.message:
            raise ValueError("PluginError.message is required")

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PluginError(Exception, _PluginErrorData):
    """Tagged plugin error usable both as a dataclass and as an Exception.

    Plugin loading / install / reload paths raise instances of this
    class. Callers that just inspect ``kind`` / ``message`` / ``details``
    treat it as a dataclass; ``except PluginError`` works because it is
    also a ``BaseException`` subclass.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        plugin_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        _PluginErrorData.__init__(
            self,
            kind=kind,
            message=message,
            plugin_name=plugin_name,
            details=details or {},
        )
        Exception.__init__(self, message)

    def __str__(self) -> str:  # legacy callers still log this
        if self.plugin_name:
            return "[%s] %s: %s" % (self.kind, self.plugin_name, self.message)
        return "[%s] %s" % (self.kind, self.message)


__all__ = [
    "CACHE_MISS",
    "DEPENDENCY_UNSATISFIED",
    "INCOMPATIBLE_API",
    "INSTALL_ERROR",
    "LOAD_ERROR",
    "LSP_FAILURE",
    "MARKETPLACE_BLOCKED",
    "MARKETPLACE_FETCH_ERROR",
    "MARKETPLACE_NOT_IMPLEMENTED",
    "MCP_FAILURE",
    "NOT_FOUND",
    "PARSE_ERROR",
    "PERMISSION_DENIED",
    "RELOAD_ERROR",
    "RETRYABLE_KINDS",
    "SIGNATURE_INVALID",
    "UNSUPPORTED_COMPONENT",
    "VALIDATION_ERROR",
    "PluginError",
]
