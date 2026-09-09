"""
Central registry for optional multimedia/UI capabilities.

Provides a single source of truth for dependency availability checks so that:
- CI can fail fast when the multimedia stack is incomplete
- Tests can mark skips/fallbacks consistently
- Code can degrade gracefully with explicit rationale
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CapabilitySpec:
    """Static descriptor for an optional capability."""

    name: str
    packages: tuple[str, ...]
    description: str
    tier: str


@dataclass(frozen=True)
class CapabilityStatus:
    """Runtime availability result for a capability."""

    name: str
    available: bool
    description: str
    tier: str
    missing_packages: tuple[str, ...]
    errors: tuple[str, ...]
    source: str

    def reason(self) -> str:
        """Return a human-friendly skip/failure reason."""
        if self.available:
            return f"Capability '{self.name}' available ({self.description})"

        error_blob = "; ".join(self.errors) if self.errors else "not installed"
        return (
            f"Capability '{self.name}' unavailable ({self.description}) - "
            f"missing packages: {', '.join(self.missing_packages)} ({error_blob})"
        )


class CapabilityUnavailable(RuntimeError):
    """Raised when a capability is required but not present."""

    def __init__(self, status: CapabilityStatus):
        super().__init__(status.reason())
        self.status = status


def _default_specs() -> dict[str, CapabilitySpec]:
    """Canonical optional capability specifications."""
    return {
        "pyqt": CapabilitySpec(
            name="pyqt",
            packages=("PyQt6", "PyQt6.QtWidgets", "PyQt6.QtCore"),
            description="Qt desktop UI stack (required for rich playback suites)",
            tier="ui",
        ),
        "librosa": CapabilitySpec(
            name="librosa",
            packages=("librosa",),
            description="Audio analysis utilities for beat tracking and audio alignment",
            tier="audio",
        ),
        "pydub": CapabilitySpec(
            name="pydub",
            packages=("pydub",),
            description="High-level audio preprocessing pipeline (normalisation, conversions)",
            tier="audio",
        ),
    }


class CapabilityRegistry:
    """Tracks optional multimedia capability availability with caching and overrides."""

    def __init__(self, specs: Mapping[str, CapabilitySpec] | None = None):
        self._specs: dict[str, CapabilitySpec] = dict(specs or _default_specs())
        self._cache: dict[str, CapabilityStatus] = {}
        self._overrides: dict[str, CapabilityStatus] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list_names(self) -> tuple[str, ...]:
        """Return the known capability names."""
        return tuple(sorted(self._specs.keys()))

    def check(self, name: str, *, refresh: bool = False) -> CapabilityStatus:
        """Return status for a capability; results are cached unless refresh=True."""
        if name not in self._specs:
            raise KeyError(f"Unknown capability '{name}'")

        if not refresh and name in self._cache:
            return self._cache[name]

        if name in self._overrides:
            status = self._overrides[name]
            self._cache[name] = status
            return status

        spec = self._specs[name]

        env_block = _parse_env_blocklist()
        if name in env_block:
            status = CapabilityStatus(
                name=name,
                available=False,
                description=spec.description,
                tier=spec.tier,
                missing_packages=spec.packages,
                errors=(f"forced missing via VIOLA_FORCE_CAPABILITY_MISSING={env_block[name]}",),
                source="env-override",
            )
            self._cache[name] = status
            return status

        missing: list[str] = []
        errors: list[str] = []

        for package in spec.packages:
            try:
                # Attempt full import to ensure transitive dependencies are satisfied.
                importlib.import_module(package)
            except Exception as exc:  # pragma: no cover - defensive logging
                missing.append(package)
                errors.append(f"{type(exc).__name__}: {exc}")

        available = not missing

        status = CapabilityStatus(
            name=name,
            available=available,
            description=spec.description,
            tier=spec.tier,
            missing_packages=tuple(missing),
            errors=tuple(errors),
            source="registry",
        )

        self._cache[name] = status

        if not available:
            logger.debug("Optional capability '%s' missing: %s", name, status.reason())

        return status

    def get_status(self, name: str, *, refresh: bool = False) -> CapabilityStatus:
        """Alias for check() - return status for a capability."""
        return self.check(name, refresh=refresh)

    def ensure(self, name: str) -> CapabilityStatus:
        """
        Ensure a capability is present, raising CapabilityUnavailable otherwise.

        Returns the CapabilityStatus when available for convenience.
        """
        status = self.check(name)
        if not status.available:
            raise CapabilityUnavailable(status)
        return status

    def missing(self) -> tuple[CapabilityStatus, ...]:
        """Return list of missing capability statuses."""
        results = []
        for name in self.list_names():
            status = self.check(name)
            if not status.available:
                results.append(status)
        return tuple(results)

    def refresh(self) -> None:
        """Drop cached status results."""
        self._cache.clear()

    @contextmanager
    def override(
        self,
        name: str,
        *,
        available: bool,
        reason: str | None = None,
        missing_packages: Iterable[str] | None = None,
    ) -> Iterator[CapabilityStatus]:
        """
        Temporarily override a capability status.

        Useful for tests that need to simulate degraded environments.
        """
        if name not in self._specs:
            raise KeyError(f"Unknown capability '{name}'")

        spec = self._specs[name]
        override_status = CapabilityStatus(
            name=name,
            available=available,
            description=spec.description,
            tier=spec.tier,
            missing_packages=tuple(missing_packages or spec.packages),
            errors=(reason or "overridden capability status",),
            source="override",
        )

        previous = self._overrides.get(name)
        self._overrides[name] = override_status
        self._cache.pop(name, None)
        try:
            yield override_status
        finally:
            if previous is None:
                self._overrides.pop(name, None)
            else:
                self._overrides[name] = previous
            self._cache.pop(name, None)


def _parse_env_blocklist() -> dict[str, str]:
    """
    Parse VIOLA_FORCE_CAPABILITY_MISSING env var into a mapping.

    Supports formats like:
        VIOLA_FORCE_CAPABILITY_MISSING=pyqt,librosa
        VIOLA_FORCE_CAPABILITY_MISSING=pyqt:ticket-123
    """
    try:
        from config.settings import settings as _app_settings

        raw = (_app_settings.force_capability_missing or "").strip()
    except Exception:
        raw = os.environ.get("VIOLA_FORCE_CAPABILITY_MISSING", "").strip()
    if not raw:
        return {}

    blocklist: dict[str, str] = {}
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if ":" in token:
            name, reason = token.split(":", 1)
            blocklist[name.strip()] = reason.strip() or "env override"
        else:
            blocklist[token] = "env override"
    return blocklist


_DEFAULT_REGISTRY = CapabilityRegistry()


def get_capability_registry() -> CapabilityRegistry:
    """Return the shared capability registry instance."""
    return _DEFAULT_REGISTRY


# Convenience helpers for callers that don't need full registry access
def check_capability(name: str) -> CapabilityStatus:
    """Check capability availability."""
    return _DEFAULT_REGISTRY.check(name)


def ensure_capability(name: str) -> CapabilityStatus:
    """Ensure capability available or raise CapabilityUnavailable."""
    return _DEFAULT_REGISTRY.ensure(name)


def missing_capabilities() -> tuple[CapabilityStatus, ...]:
    """Convenience wrapper to list missing capabilities."""
    return _DEFAULT_REGISTRY.missing()


__all__ = [
    "CapabilityRegistry",
    "CapabilitySpec",
    "CapabilityStatus",
    "CapabilityUnavailable",
    "check_capability",
    "ensure_capability",
    "get_capability_registry",
    "missing_capabilities",
]
