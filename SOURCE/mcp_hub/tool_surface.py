"""Canonical runtime tool-surface model.

This captures every meaningful view of the tool payload for a single
agent turn: the full hub-visible surface, the provider-visible payload,
and the step-log view.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _copy_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(tool, default=str))


def _copy_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [_copy_tool(tool) for tool in tools or []]


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [str(tool.get("name", "")).strip() for tool in tools if str(tool.get("name", "")).strip()]


@dataclass(frozen=True)
class ToolSurface:
    """Immutable runtime snapshot of the agent tool surface."""

    hub_visible: list[dict[str, Any]]
    provider_native: list[dict[str, Any]] = field(default_factory=list)
    step_log_visible: list[dict[str, Any]] = field(default_factory=list)
    hidden_reasons: dict[str, list[str]] = field(default_factory=dict)
    collapsed_from: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    fingerprint: str = ""

    @classmethod
    def build(
        cls,
        *,
        hub_visible: list[dict[str, Any]],
        hidden_reasons: dict[str, list[str]] | None = None,
        collapsed_from: dict[str, list[str]] | None = None,
        provider_native: list[dict[str, Any]] | None = None,
        step_log_visible: list[dict[str, Any]] | None = None,
    ) -> ToolSurface:
        hub_copy = _copy_tools(hub_visible)
        provider_copy = _copy_tools(provider_native) if provider_native is not None else _copy_tools(hub_visible)
        step_copy = _copy_tools(step_log_visible) if step_log_visible is not None else _copy_tools(provider_copy)

        normalized_hidden: dict[str, list[str]] = {}
        for name, reasons in (hidden_reasons or {}).items():
            unique = sorted({str(reason).strip() for reason in reasons if str(reason).strip()})
            if unique:
                normalized_hidden[str(name)] = unique

        collapsed_copy = {
            str(name): sorted({str(child).strip() for child in children if str(child).strip()})
            for name, children in (collapsed_from or {}).items()
            if children
        }

        counts = {
            "hub_visible": len(hub_copy),
            "provider_native": len(provider_copy),
            "step_log_visible": len(step_copy),
            "hidden_tools": len(normalized_hidden),
            "collapsed_parents": len(collapsed_copy),
        }

        payload = {
            "hub_visible_names": _tool_names(hub_copy),
            "provider_native_names": _tool_names(provider_copy),
            "step_log_visible_names": _tool_names(step_copy),
            "hidden_reasons": normalized_hidden,
            "collapsed_from": collapsed_copy,
            "counts": counts,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:16]

        return cls(
            hub_visible=hub_copy,
            provider_native=provider_copy,
            step_log_visible=step_copy,
            hidden_reasons=normalized_hidden,
            collapsed_from=collapsed_copy,
            counts=counts,
            fingerprint=fingerprint,
        )

    def with_provider_native(self, provider_native: list[dict[str, Any]]) -> ToolSurface:
        """Return a copy with an updated provider-visible payload."""

        return ToolSurface.build(
            hub_visible=self.hub_visible,
            hidden_reasons=self.hidden_reasons,
            collapsed_from=self.collapsed_from,
            provider_native=provider_native,
            step_log_visible=provider_native,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "hub_visible": _copy_tools(self.hub_visible),
            "provider_native": _copy_tools(self.provider_native),
            "step_log_visible": _copy_tools(self.step_log_visible),
            "hidden_reasons": json.loads(json.dumps(self.hidden_reasons)),
            "collapsed_from": json.loads(json.dumps(self.collapsed_from)),
            "counts": dict(self.counts),
            "fingerprint": self.fingerprint,
        }
