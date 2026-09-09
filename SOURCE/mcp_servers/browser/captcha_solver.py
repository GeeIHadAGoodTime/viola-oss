"""CAPTCHA handling — site-pivot strategy.

Viola does NOT solve CAPTCHAs.  When bot protection is detected, the agent
pivots to an alternative site that offers the same product or information.
This module is intentionally empty; it exists only to avoid import errors
from any code that may still reference it during cleanup.

server.py surfaces bot-protection page facts to the caller.
"""
