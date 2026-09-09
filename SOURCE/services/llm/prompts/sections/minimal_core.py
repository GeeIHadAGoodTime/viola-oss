"""Compatibility alias for Viola's single static prompt doctrine.

The active agent path owns static doctrine in ``VIOLA_UNIFIED_PROMPT``.
Older tests and imports still reference ``MINIMAL_CORE``; keep them pointed at
the same prompt body so this module cannot drift into a competing prompt.
"""

from __future__ import annotations

from services.llm.prompts.viola_unified import VIOLA_UNIFIED_PROMPT

_MAX_CORE_TOKENS = 5000

MINIMAL_CORE = VIOLA_UNIFIED_PROMPT

_token_estimate = len(MINIMAL_CORE) // 4
assert (
    _token_estimate <= _MAX_CORE_TOKENS
), "MINIMAL_CORE is %d tokens (max %d). Keep MINIMAL_CORE aligned with VIOLA_UNIFIED_PROMPT." % (
    _token_estimate,
    _MAX_CORE_TOKENS,
)
