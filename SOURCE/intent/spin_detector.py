"""Spin detection for agent tool-use loops.

Simplified to observability counters.

Detection modes
---------------
**Identical-call repetition**: consecutive successful calls to the same tool
with the same input arguments are logged for diagnostics.

Wall-clock timeouts and managed spend caps handle overall bounds. This module
does not block tool calls, inject correction text, or force termination;
tool-choice guidance belongs in prompts and tool descriptions.
"""

from __future__ import annotations

import json

from core.logging_config import get_logger

logger = get_logger(__name__)


class SpinDetector:
    """Observability-only tracker for repeated tool calls.

    Historical versions injected correction messages and mechanically blocked
    repeated calls. Current behavior records enough state for logs and tests
    without constraining the model's tool choice.
    """

    def __init__(
        self,
        max_retries: int = 3,
        window_size: int = 12,
        max_consecutive_observational: int = 20,
        max_consecutive_identical: int = 3,
        max_same_tool_dominance: int = 6,
        max_web_search_without_read: int = 4,
    ) -> None:
        # Keep constructor signature for backward compat. Same-tool and search
        # counters are observability only; they must not steer tool choice.
        self.max_retries = max_retries
        self.window_size = window_size
        self.max_consecutive_observational = max_consecutive_observational
        self.max_consecutive_identical = max_consecutive_identical
        self.max_same_tool_dominance = max_same_tool_dominance
        self.max_web_search_without_read = max_web_search_without_read

        self._interventions_fired: int = 0

        # Identical-call tracking.
        self._consecutive_identical: int = 0
        self._last_success_sig: str = ""

        # Consecutive same-tool tracking for logs/diagnostics only.
        self._consecutive_same_tool: int = 0
        self._last_success_tool: str = ""
        self._same_tool_unique_sigs: set[str] = set()

        # Search/read observations retained for diagnostics and compatibility.
        self._web_searches_since_read: int = 0
        self._web_search_unique_sigs_since_read: set[str] = set()

        # Historical call-blocking state retained for compatibility; current
        # behavior never adds entries.
        self._blocked_calls: set[str] = set()

    _MAX_IGNORED_INTERVENTIONS: int = 3

    def reset_session(self) -> None:
        """Reset per-session counters. Call at the start of each new agent task."""
        self._interventions_fired = 0
        self._consecutive_identical = 0
        self._last_success_sig = ""
        self._consecutive_same_tool = 0
        self._last_success_tool = ""
        self._same_tool_unique_sigs.clear()
        self._web_searches_since_read = 0
        self._web_search_unique_sigs_since_read.clear()
        self._blocked_calls.clear()
        logger.debug("SpinDetector: session reset")

    @staticmethod
    def _stringify_tool_args(tool_args: object) -> str:
        """Return a stable, bounded string representation of tool args."""
        if tool_args is None:
            return ""
        if isinstance(tool_args, str):
            return tool_args[:200]
        try:
            return json.dumps(tool_args, sort_keys=True, default=str)[:200]
        except TypeError:
            return str(tool_args)[:200]

    @staticmethod
    def _normalize_page_url(page_url: str | None) -> str:
        """Return a stable page identifier for browser-tool signatures."""
        if not page_url:
            return ""
        text = str(page_url).strip()
        if not text:
            return ""
        return text[-120:]

    def normalize_input_signature(self, tool: str, tool_args: object = "", page_url: str | None = None) -> str:
        """Return the canonical signature used for spin tracking compatibility."""
        args_sig = self._stringify_tool_args(tool_args)
        page_sig = self._normalize_page_url(page_url) if tool.startswith("browser_") else ""
        if page_sig:
            return "%s|%s" % (args_sig, page_sig)
        return args_sig

    def record_failure(self, tool: str, input_summary: object, error: str, *, page_url: str | None = None) -> None:
        """Record a tool failure. Breaks the identical-call streak."""
        self._consecutive_identical = 0
        self._last_success_sig = ""
        self._consecutive_same_tool = 0
        self._last_success_tool = ""
        self._same_tool_unique_sigs.clear()
        self._web_searches_since_read = 0
        self._web_search_unique_sigs_since_read.clear()
        logger.debug("Spin: FAIL %s", tool)

    def record_success(self, tool: str, input_sig: object = "", *, page_url: str | None = None) -> None:
        """Record a tool success. Tracks repeated calls for diagnostics."""
        normalized_sig = self.normalize_input_signature(tool, input_sig, page_url=page_url)

        sig = "%s:%s" % (tool, normalized_sig)
        if sig == self._last_success_sig:
            self._consecutive_identical += 1
        else:
            self._consecutive_identical = 1
            self._last_success_sig = sig

        if tool == self._last_success_tool:
            self._consecutive_same_tool += 1
            self._same_tool_unique_sigs.add(normalized_sig)
        else:
            self._consecutive_same_tool = 1
            self._last_success_tool = tool
            self._same_tool_unique_sigs = {normalized_sig}

        if tool == "web_search":
            self._web_searches_since_read += 1
            self._web_search_unique_sigs_since_read.add(normalized_sig)
        elif tool == "web_read":
            self._web_searches_since_read = 0
            self._web_search_unique_sigs_since_read.clear()

        logger.debug(
            "Spin: OK %s (consec_ident=%d consec_same_tool=%d search_since_read=%d)",
            tool,
            self._consecutive_identical,
            self._consecutive_same_tool,
            self._web_searches_since_read,
        )

    def check_tool_redirect(self, tool: str, available_tools: set[str] | None) -> str | None:
        """Stub: tool redirect detection removed. Always returns None."""
        return None

    def is_call_blocked(self, tool: str, args_sig: object, *, page_url: str | None = None) -> str | None:
        """Return None; repeated-call blocking was removed."""
        return None

    @property
    def should_force_terminate(self) -> bool:
        """Return False; spin-based force termination was removed."""
        return False

    def is_spinning(self) -> str | None:
        """Observe repeated calls and return None.

        The agent may legitimately repeat tools while gathering evidence.
        Repetition is useful diagnostic signal, not a reason to steer or block.
        """
        if self._consecutive_identical >= self.max_consecutive_identical:
            tool_name = self._last_success_sig.split(":", 1)[0] if self._last_success_sig else "unknown"
            logger.info(
                "Identical-call repetition observed: %s called %d times with same input",
                tool_name,
                self._consecutive_identical,
            )

        if self._web_searches_since_read >= self.max_web_search_without_read:
            logger.info(
                "Search-without-read pattern observed: web_search called %d times before web_read",
                self._web_searches_since_read,
            )

        if (
            self._last_success_tool == "web_search"
            and self._consecutive_same_tool >= self.max_same_tool_dominance
            and len(self._same_tool_unique_sigs) >= min(3, self.max_same_tool_dominance)
        ):
            logger.info(
                "Same-tool search repetition observed: web_search called %d times with varied inputs",
                self._consecutive_same_tool,
            )

        return None
