"""Model fallback tracker with configurable fallback chains.

Provides B4 (Configurable Fallback Chains) on top of the existing
model fallback tracker.  Users can define ``llm_fallback_chain`` in
settings.json as a list of model identifiers.  The chain is walked
in order when consecutive failures exceed the threshold.

Default chain: ["gpt-5.4-mini", "claude-haiku-4-5-20251001", "gemini-2.0-flash"]

Each model identifier is parsed as ``<model>`` or ``<provider>:<model>``
(e.g. ``ollama:llama3``).  When no provider prefix is given, it is
inferred from the model name.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

MAX_CONSECUTIVE_FAILURES = 3
RETRY_INTERVAL_SECONDS = 300  # 5 minutes
RETRY_REQUEST_INTERVAL = 10  # every 10th request while on fallback

# HTTP status codes that indicate the model is permanently unavailable
# (not retriable — skip immediately to the next model in the chain)
_MODEL_ACCESS_STATUS_CODES = frozenset({403, 404})

# Legacy constants kept for backward compatibility
PRIMARY_MODEL = "gpt-5.4-mini"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
FALLBACK_PROVIDER = "anthropic"

# Default fallback chain — used when no user configuration is present
DEFAULT_FALLBACK_CHAIN: list[str] = [
    "gpt-5.4-mini",
    "claude-haiku-4-5-20251001",
    "gemini-2.0-flash",
]


# ---------------------------------------------------------------------------
# B4: Chain entry parsing
# ---------------------------------------------------------------------------


@dataclass
class _ChainEntry:
    """A single entry in the fallback chain."""

    model: str
    provider: str | None  # None = auto-detect

    @classmethod
    def parse(cls, raw: str) -> _ChainEntry:
        """Parse ``provider:model`` or just ``model``."""
        raw = raw.strip()
        if ":" in raw and not raw.startswith("ollama:"):
            # Handle provider:model except for ollama: which uses tags
            parts = raw.split(":", 1)
            if parts[0] in ("openai", "anthropic", "google", "ollama", "openai_compatible"):
                return cls(model=parts[1], provider=parts[0])
        # Auto-detect provider from model name
        return cls(model=raw, provider=_infer_provider(raw))


def _infer_provider(model: str) -> str | None:
    """Infer provider from model name prefix."""
    lower = model.lower()
    if lower.startswith("gpt-") or lower.startswith("o1-") or lower.startswith("o3-"):
        return "openai"
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("gemini"):
        return "google"
    if lower.startswith("llama") or lower.startswith("mistral") or lower.startswith("phi"):
        return "ollama"
    return None


def _is_model_not_found(error: Exception) -> bool:
    """Return True if *error* indicates the model is permanently unavailable.

    Matches two patterns:
    1. OpenAI-style ``APIStatusError`` with ``status_code`` 403 or 404.
    2. Any exception whose string representation contains ``model_not_found``.

    These errors are non-retriable — retrying the same model will always fail
    (e.g. gpt-5.4 sent via a BYOK key that doesn't have access to it).
    """
    error_str = str(error).lower()

    # Pattern 1: explicit "model_not_found" anywhere in the error text
    if "model_not_found" in error_str:
        return True

    # Pattern 2: HTTP 403/404 with model-related language
    status_code = getattr(error, "status_code", None)
    if status_code in _MODEL_ACCESS_STATUS_CODES:
        # Only treat as model-not-found if the message mentions model access,
        # not a generic auth failure (e.g. invalid API key is also 403).
        _model_keywords = ("model", "not found", "does not exist", "not available", "access")
        if any(kw in error_str for kw in _model_keywords):
            return True

    return False


def _load_fallback_chain() -> list[_ChainEntry]:
    """Load the fallback chain from SettingsManager or use defaults.

    The chain's primary model is derived from the user's actual LLM
    configuration (settings or Codex defaults) so it stays in sync
    with ``create_from_settings``.  Previously it was hardcoded to
    ``gpt-5.4-mini`` which broke when the user switched to a different
    default model (e.g. Codex subscription with gpt-5.4).
    """
    chain_raw: list[str] | None = None
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        raw = sm.get("llm_fallback_chain")
        if isinstance(raw, list) and raw:
            chain_raw = [str(x) for x in raw if x]
    except Exception:
        logger.debug("Could not load llm_fallback_chain from SettingsManager, using defaults")

    if not chain_raw:
        # Build a default chain whose primary model matches the user's
        # actual provider configuration instead of always starting with
        # the hardcoded DEFAULT_FALLBACK_CHAIN[0].
        primary_model = DEFAULT_FALLBACK_CHAIN[0]  # gpt-5.4-mini
        try:
            from config.defaults import DEFAULT_AI_SOURCE, resolve_effective_model
            from ui.settings_manager import get_settings_manager as _gsm

            _sm = _gsm()
            _ai_source_raw = _sm.get("ai_source", DEFAULT_AI_SOURCE)
            _ai_source = _ai_source_raw if isinstance(_ai_source_raw, str) else DEFAULT_AI_SOURCE
            _provider_raw = _sm.get("llm_provider", "openai")
            _provider = _provider_raw if isinstance(_provider_raw, str) and _provider_raw else "openai"
            primary_model = resolve_effective_model(
                ai_source=_ai_source,
                provider=_provider,
                agent=False,
                candidates=(_sm.get("llm_model", ""),),
                fallback=primary_model,
            )
        except Exception:
            logger.debug("Could not read primary model from settings, using default")
        # Replace the hardcoded primary with the user's actual model,
        # keeping the rest of the fallback chain intact.
        chain_raw = [primary_model] + list(DEFAULT_FALLBACK_CHAIN[1:])

    entries = [_ChainEntry.parse(r) for r in chain_raw]
    logger.info(
        "Fallback chain: %s",
        " -> ".join("%s(%s)" % (e.model, e.provider or "auto") for e in entries),
    )
    return entries


@dataclass
class ModelFallbackTracker:
    """Tracks model failures and manages fallback through a configurable chain.

    After ``MAX_CONSECUTIVE_FAILURES`` consecutive failures of the current
    model, the tracker advances to the next model in the chain.  While on
    a fallback model it periodically signals the caller to retry the
    primary (first-in-chain) model.  When a primary-model call succeeds
    the tracker auto-recovers.
    """

    _consecutive_failures: int = 0
    _is_fallback_active: bool = False
    _last_retry_time: float = 0.0
    _total_fallback_activations: int = 0
    _requests_since_last_retry: int = 0
    _current_chain_index: int = 0
    _chain: list[_ChainEntry] = field(default_factory=list)
    _bug_reports_dir: Path = field(
        default_factory=lambda: Path.cwd() / "data" / "bug_reports",
    )

    def __post_init__(self) -> None:
        if not self._chain:
            self._chain = _load_fallback_chain()

    # ------------------------------------------------------------------ #
    # Public properties
    # ------------------------------------------------------------------ #

    @property
    def is_fallback_active(self) -> bool:
        """Return True when a fallback model is in use."""
        return self._is_fallback_active

    @property
    def current_model(self) -> str:
        """Return the model that should be used for the next request."""
        if self._chain:
            idx = min(self._current_chain_index, len(self._chain) - 1)
            return self._chain[idx].model
        return PRIMARY_MODEL

    @property
    def current_provider(self) -> str | None:
        """Return provider override when fallback is active, None otherwise."""
        if not self._is_fallback_active:
            return None
        if self._chain:
            idx = min(self._current_chain_index, len(self._chain) - 1)
            return self._chain[idx].provider
        return FALLBACK_PROVIDER

    @property
    def consecutive_failures(self) -> int:
        """Return current consecutive failure count (for diagnostics)."""
        return self._consecutive_failures

    @property
    def chain_position(self) -> int:
        """Current position in the fallback chain (0 = primary)."""
        return self._current_chain_index

    @property
    def chain_length(self) -> int:
        """Total number of models in the fallback chain."""
        return len(self._chain)

    # ------------------------------------------------------------------ #
    # Recording outcomes
    # ------------------------------------------------------------------ #

    def record_success(self) -> None:
        """Record a successful model call.  Resets failure count and deactivates fallback."""
        if self._is_fallback_active:
            logger.info(
                "Primary model %s recovered, deactivating fallback (was at chain pos %d)",
                self.current_model,
                self._current_chain_index,
            )
            self._is_fallback_active = False
            self._current_chain_index = 0
        self._consecutive_failures = 0
        self._requests_since_last_retry = 0

    def record_failure(self, error: Exception) -> bool:
        """Record a model failure.

        Returns:
            True if fallback was *just* activated or advanced by this call.
        """
        self._consecutive_failures += 1

        # S9-FINDING-2: Non-retriable model errors (model_not_found, 403/404
        # with model-access language) skip retries and advance immediately.
        skip_immediately = _is_model_not_found(error)
        if skip_immediately:
            logger.warning(
                "Model %s got non-retriable error (model_not_found), skipping immediately: %s",
                self.current_model,
                type(error).__name__,
            )
        else:
            logger.warning(
                "Model %s failure %d/%d: %s",
                self.current_model,
                self._consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
                type(error).__name__,
            )

        if skip_immediately or self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # Advance in the chain
            next_idx = self._current_chain_index + 1
            if next_idx < len(self._chain):
                self._current_chain_index = next_idx
                self._is_fallback_active = True
                self._total_fallback_activations += 1
                self._last_retry_time = time.monotonic()
                self._requests_since_last_retry = 0
                self._consecutive_failures = 0  # Reset for the new model
                logger.warning(
                    "Advancing to fallback chain[%d] = %s (activation #%d)",
                    next_idx,
                    self._chain[next_idx].model,
                    self._total_fallback_activations,
                )
                self._file_bug_report(error)
                return True
            elif not self._is_fallback_active:
                # Already at end of chain, activate fallback flag
                self._is_fallback_active = True
                self._total_fallback_activations += 1
                self._last_retry_time = time.monotonic()
                self._requests_since_last_retry = 0
                logger.warning(
                    "All models in fallback chain exhausted after %d failures (activation #%d)",
                    self._consecutive_failures,
                    self._total_fallback_activations,
                )
                self._file_bug_report(error)
                return True
        return False

    def force_active(self, *, reason: str = "") -> None:
        """Force fallback mode for an upstream policy-triggered model swap."""

        if self._chain and self._current_chain_index == 0 and len(self._chain) > 1:
            self._current_chain_index = 1
        self._is_fallback_active = True
        self._total_fallback_activations += 1
        self._last_retry_time = time.monotonic()
        self._requests_since_last_retry = 0
        self._consecutive_failures = 0
        logger.warning(
            "Fallback forced active%s at chain[%d]=%s",
            " (%s)" % reason if reason else "",
            self._current_chain_index,
            self.current_model,
        )

    # ------------------------------------------------------------------ #
    # Retry logic
    # ------------------------------------------------------------------ #

    def should_retry_primary(self) -> bool:
        """Check whether we should probe the primary model on this request.

        Two triggers (whichever fires first):
        1. ``RETRY_INTERVAL_SECONDS`` (5 min) since the last retry attempt.
        2. Every ``RETRY_REQUEST_INTERVAL`` (10) requests on fallback.
        """
        if not self._is_fallback_active:
            return False

        self._requests_since_last_retry += 1

        # Time-based trigger
        elapsed = time.monotonic() - self._last_retry_time
        if elapsed >= RETRY_INTERVAL_SECONDS:
            self._last_retry_time = time.monotonic()
            self._requests_since_last_retry = 0
            logger.info(
                "Retrying primary model %s after %.0fs on fallback",
                self._chain[0].model if self._chain else PRIMARY_MODEL,
                elapsed,
            )
            return True

        # Request-count trigger
        if self._requests_since_last_retry >= RETRY_REQUEST_INTERVAL:
            self._last_retry_time = time.monotonic()
            self._requests_since_last_retry = 0
            logger.info(
                "Retrying primary model %s after %d requests on fallback",
                self._chain[0].model if self._chain else PRIMARY_MODEL,
                RETRY_REQUEST_INTERVAL,
            )
            return True

        return False

    # ------------------------------------------------------------------ #
    # Chain management
    # ------------------------------------------------------------------ #

    def reload_chain(self) -> None:
        """Reload the fallback chain from settings (e.g. after user edit)."""
        self._chain = _load_fallback_chain()
        # Clamp current index
        if self._current_chain_index >= len(self._chain):
            self._current_chain_index = max(0, len(self._chain) - 1)

    def get_chain_info(self) -> list[dict[str, Any]]:
        """Return chain info for diagnostics."""
        result: list[dict[str, Any]] = []
        for i, entry in enumerate(self._chain):
            result.append(
                {
                    "index": i,
                    "model": entry.model,
                    "provider": entry.provider,
                    "active": i == self._current_chain_index,
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # Bug reporting
    # ------------------------------------------------------------------ #

    def _file_bug_report(self, error: Exception) -> None:
        """File a JSON bug report when fallback activates."""
        try:
            self._bug_reports_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(UTC)
            current = self._chain[self._current_chain_index] if self._chain else None
            report: dict[str, Any] = {
                "source": "model_fallback",
                "created_at": now.isoformat(),
                "pattern": "Model consecutive failures — chain advancement",
                "frequency": self._consecutive_failures,
                "suggested_fix": "Check API key billing/quota and model availability",
                "context": {
                    "current_model": current.model if current else PRIMARY_MODEL,
                    "current_provider": current.provider if current else None,
                    "chain_position": self._current_chain_index,
                    "chain_length": len(self._chain),
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                    "activation_number": self._total_fallback_activations,
                },
                "status": "open",
            }
            filename = "model_fallback_%s.json" % now.strftime("%Y%m%d_%H%M%S")
            path = self._bug_reports_dir / filename
            path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Bug report filed: %s", filename)
        except Exception:
            logger.exception("Failed to file bug report for model fallback")


# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #

_fallback_tracker: ModelFallbackTracker | None = None


def get_fallback_tracker() -> ModelFallbackTracker:
    """Get or create the singleton fallback tracker."""
    global _fallback_tracker
    if _fallback_tracker is None:
        _fallback_tracker = ModelFallbackTracker()
    return _fallback_tracker
