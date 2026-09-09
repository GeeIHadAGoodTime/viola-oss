"""Agent self-diagnosis subsystem.

Provides:
- Preflight validation (catch config issues before LLM calls)
- Diagnostic context capture (structured failure snapshots)
- Self-diagnosis engine (LLM-powered failure analysis)
- Bug ticket persistence (SQLite-backed issue tracking)
"""
