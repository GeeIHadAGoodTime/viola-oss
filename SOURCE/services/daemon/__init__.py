"""Viola daemon mode -- headless API server without Qt UI.

Starts the API server, messaging channels, scheduler, and optionally
the audio pipeline, all without requiring a display or Qt installation.

Entry point: ``python -m services.daemon.viola_daemon``
"""

from __future__ import annotations

__all__: list[str] = []
