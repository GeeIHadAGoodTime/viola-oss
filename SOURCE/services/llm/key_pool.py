"""Multi-key API rotation with per-key exponential backoff.

Implements B2 (Multi-Key API Rotation) and B5 (Per-Key Exponential Backoff).

Keys are loaded from comma-separated environment variables (e.g.
``OPENAI_API_KEYS=sk-1,sk-2``) falling back to the single-key variable
(``OPENAI_API_KEY``).  The pool tracks per-key state and selects the
least-recently-used key that is not in cooldown.

Backoff escalation:
    Standard errors:  1min -> 5min -> 25min -> 1h
    Billing (402):    5h initial (single stage, then advance provider)

Counters reset after 24 hours without failures.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Backoff stages in seconds: 1min, 5min, 25min, 1h
_STANDARD_BACKOFF_STAGES = (60.0, 300.0, 1500.0, 3600.0)
_BILLING_BACKOFF_INITIAL = 18000.0  # 5 hours
_FAILURE_RESET_WINDOW = 86400.0  # 24 hours


@dataclass
class _KeyState:
    """Per-key tracking state."""

    key: str
    last_used: float = 0.0
    cooldown_until: float = 0.0
    failure_count: int = 0
    last_failure_time: float = 0.0
    is_billing_error: bool = False

    def is_available(self, now: float | None = None) -> bool:
        """Return True if this key is not in cooldown."""
        now = now or time.monotonic()
        # Reset failure count after 24h without failures
        if (
            self.failure_count > 0
            and self.last_failure_time > 0
            and (now - self.last_failure_time) > _FAILURE_RESET_WINDOW
        ):
            self.failure_count = 0
            self.cooldown_until = 0.0
            self.is_billing_error = False
        return now >= self.cooldown_until

    def record_success(self, now: float | None = None) -> None:
        """Record a successful use.  Resets failure state."""
        now = now or time.monotonic()
        self.last_used = now
        self.failure_count = 0
        self.cooldown_until = 0.0
        self.is_billing_error = False

    def record_failure(
        self,
        *,
        is_billing: bool = False,
        now: float | None = None,
    ) -> None:
        """Record a failure and compute the next cooldown."""
        now = now or time.monotonic()
        self.failure_count += 1
        self.last_failure_time = now

        if is_billing:
            self.is_billing_error = True
            self.cooldown_until = now + _BILLING_BACKOFF_INITIAL
            logger.warning(
                "Key %s hit billing error, cooldown for %.0fs",
                _mask_key(self.key),
                _BILLING_BACKOFF_INITIAL,
            )
        else:
            stage_idx = min(self.failure_count - 1, len(_STANDARD_BACKOFF_STAGES) - 1)
            delay = _STANDARD_BACKOFF_STAGES[stage_idx]
            self.cooldown_until = now + delay
            logger.warning(
                "Key %s failure #%d, cooldown for %.0fs",
                _mask_key(self.key),
                self.failure_count,
                delay,
            )

    def force_retry(self) -> None:
        """User override: clear cooldown immediately."""
        self.cooldown_until = 0.0


def _mask_key(key: str) -> str:
    """Mask API key for safe logging."""
    if len(key) <= 8:
        return "***"
    return "%s...%s" % (key[:4], key[-4:])


class KeyPool:
    """Manages a pool of API keys with rotation and per-key backoff.

    Usage::

        pool = KeyPool.from_env("OPENAI_API_KEYS", fallback_var="OPENAI_API_KEY")
        key = pool.get_key()     # LRU key not in cooldown
        pool.report_success(key) # on success
        pool.report_failure(key, is_billing=False)  # on failure

    Args:
        keys: List of API key strings.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for k in keys:
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                unique.append(k)
        if not unique:
            raise ValueError("KeyPool requires at least one non-empty key")
        self._states: list[_KeyState] = [_KeyState(key=k) for k in unique]
        logger.info("KeyPool initialized with %d key(s)", len(self._states))

    @classmethod
    def from_env(
        cls,
        multi_var: str,
        fallback_var: str | None = None,
        fallback_value: str | None = None,
    ) -> KeyPool | None:
        """Create a KeyPool from environment variables.

        Tries *multi_var* first (comma-separated), then *fallback_var*
        (single key), then *fallback_value*.  Returns ``None`` if no
        keys are found.

        Args:
            multi_var: Env var name for comma-separated keys.
            fallback_var: Env var name for single key fallback.
            fallback_value: Direct string fallback (e.g. from settings).
        """
        raw = os.environ.get(multi_var, "").strip()
        if raw:
            keys = [k.strip() for k in raw.split(",") if k.strip()]
            if keys:
                return cls(keys)

        if fallback_var:
            single = os.environ.get(fallback_var, "").strip()
            if single:
                return cls([single])

        if fallback_value and fallback_value.strip():
            # Also handle comma-separated in the fallback value
            keys = [k.strip() for k in fallback_value.split(",") if k.strip()]
            if keys:
                return cls(keys)

        return None

    @property
    def size(self) -> int:
        """Number of keys in the pool."""
        return len(self._states)

    def get_key(self) -> str | None:
        """Select the best available key (LRU, not in cooldown).

        Returns:
            An API key string, or ``None`` if all keys are in cooldown.
        """
        now = time.monotonic()
        available = [s for s in self._states if s.is_available(now)]
        if not available:
            logger.warning("All %d key(s) in cooldown", len(self._states))
            return None
        # Sort by last_used ascending (least recently used first)
        available.sort(key=lambda s: s.last_used)
        chosen = available[0]
        chosen.last_used = now
        return chosen.key

    def report_success(self, key: str) -> None:
        """Record a successful API call for *key*."""
        state = self._find(key)
        if state:
            state.record_success()

    def report_failure(self, key: str, *, is_billing: bool = False) -> None:
        """Record a failed API call for *key*.

        Args:
            key: The API key that failed.
            is_billing: True if the error was a billing/payment (402) error.
        """
        state = self._find(key)
        if state:
            state.record_failure(is_billing=is_billing)

    def force_retry(self, key: str | None = None) -> None:
        """Clear cooldown for *key*, or all keys if ``None``.

        User override to allow immediate retry regardless of backoff.
        """
        if key is None:
            for state in self._states:
                state.force_retry()
            logger.info("Force-cleared cooldown for all %d key(s)", len(self._states))
        else:
            state = self._find(key)
            if state:
                state.force_retry()
                logger.info("Force-cleared cooldown for key %s", _mask_key(key))

    def get_diagnostics(self) -> list[dict[str, Any]]:
        """Return per-key diagnostics for observability."""
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for s in self._states:
            result.append(
                {
                    "key_prefix": _mask_key(s.key),
                    "available": s.is_available(now),
                    "failure_count": s.failure_count,
                    "is_billing_error": s.is_billing_error,
                    "cooldown_remaining_s": max(0.0, s.cooldown_until - now),
                }
            )
        return result

    def _find(self, key: str) -> _KeyState | None:
        for s in self._states:
            if s.key == key:
                return s
        return None


# ---------------------------------------------------------------------------
# Module-level singletons per provider
# ---------------------------------------------------------------------------

_pools: dict[str, KeyPool] = {}


def get_key_pool(provider: str) -> KeyPool | None:
    """Get or create the KeyPool singleton for a provider.

    Supported providers: ``openai``, ``anthropic``, ``google``.

    Returns ``None`` if no keys are configured for the provider.
    """
    if provider in _pools:
        return _pools[provider]

    from config.settings import settings

    pool: KeyPool | None = None

    if provider in ("openai", "openai_compatible"):
        pool = KeyPool.from_env(
            "OPENAI_API_KEYS",
            fallback_var="OPENAI_API_KEY",
            fallback_value=settings.openai_api_key,
        )
    elif provider == "anthropic":
        pool = KeyPool.from_env(
            "ANTHROPIC_API_KEYS",
            fallback_var="ANTHROPIC_API_KEY",
            fallback_value=settings.anthropic_api_key,
        )
    elif provider == "google":
        pool = KeyPool.from_env(
            "GOOGLE_API_KEYS",
            fallback_var="GOOGLE_API_KEY",
            fallback_value=settings.google_api_key,
        )

    if pool is not None:
        _pools[provider] = pool
    return pool
