"""
Rate limiter for API calls.

Shared request rate limiting utilities.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """Simple rate limiter for API calls."""

    max_calls: int = 50  # Max calls per window
    window_seconds: int = 60  # Window duration
    calls: list[float] = field(default_factory=list)  # Call timestamps

    def check_rate_limit(self) -> bool:
        """Check if we're within rate limits. Returns True if OK, False if limited."""
        now = time.time()

        # Remove old calls outside window
        cutoff = now - self.window_seconds
        self.calls = [t for t in self.calls if t > cutoff]

        # Check if we're at limit
        if len(self.calls) >= self.max_calls:
            return False

        # Record this call
        self.calls.append(now)
        return True

    def reset(self) -> None:
        """Reset rate limiter."""
        self.calls.clear()
