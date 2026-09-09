"""
Security Module
===============

Unified security utilities for Viola.

Provides:
- SecureApiKey: Prevents API key leakage in logs
- SecurityAudit: Automated security auditing
- Input validation helpers
"""

from __future__ import annotations

from utils.security.audit_tool import SecurityAudit
from utils.security.secure_api_key import SecureApiKey, secure_key

__all__ = [
    "SecureApiKey",
    "SecurityAudit",
    "secure_key",
]
