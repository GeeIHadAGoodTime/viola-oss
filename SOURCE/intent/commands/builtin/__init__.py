"""Claude-compatible built-in slash command implementations.

Each module exposes a ``command_spec(...)`` constructor that returns a
configured :class:`CommandSpec`. ``intent.pipeline._register_builtin_commands``
wires them into the registry so command parsing finds them like any other
``/<name>`` invocation.

Handlers use the signatures
``(invocation, *, pipeline) -> dict[str, object] | Awaitable[...]`` so the
pipeline binds ``pipeline=self`` at registration time without subclassing.
"""

from __future__ import annotations

from intent.commands.builtin.agents import command_spec as agents_command_spec
from intent.commands.builtin.clear import command_spec as clear_command_spec
from intent.commands.builtin.compact import command_spec as compact_command_spec
from intent.commands.builtin.model import command_spec as model_command_spec
from intent.commands.builtin.permissions import command_spec as permissions_command_spec

__all__ = [
    "agents_command_spec",
    "clear_command_spec",
    "compact_command_spec",
    "model_command_spec",
    "permissions_command_spec",
]
