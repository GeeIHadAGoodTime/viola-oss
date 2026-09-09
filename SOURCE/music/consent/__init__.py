"""
Public entrypoint for the consent orchestrator.

Usage:
    from music.consent import get_consent_service
    consent = get_consent_service()
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from music.consent.rotation import TokenRotationJob
from music.consent.service import ConsentService
from music.consent.vault import EncryptedTokenVault

_T = TypeVar("_T")

try:
    from utils.singleton import create_singleton_getter
except ImportError:  # pragma: no cover - fallback for constrained environments

    def create_singleton_getter(key: str, factory: Callable[[], _T]) -> Callable[[], _T]:
        _instance: _T | None = None

        def getter() -> _T:
            nonlocal _instance
            if _instance is None:
                _instance = factory()
            return _instance

        return getter


def _build_service() -> ConsentService:
    vault = EncryptedTokenVault()
    service = ConsentService(vault=vault)
    return service


get_consent_service: Callable[[], ConsentService] = create_singleton_getter(
    "consent_service",
    _build_service,
)


__all__ = [
    "ConsentService",
    "EncryptedTokenVault",
    "TokenRotationJob",
    "get_consent_service",
]
