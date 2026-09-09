from __future__ import annotations

"""
Repo-wide contract helpers.

Import submodules directly (e.g. `contracts.api_response`, `contracts.player_state`)
to avoid import-time cycles between the canonical models and the contract validation layer.

NOTE: JSON types are now in `core.json_types` (canonical module).
"""

__all__: list[str] = []
