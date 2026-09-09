"""Canonical command registry for Viola parity command surfaces."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
)

CommandHandler = Callable[["CommandInvocation"], Any]
CommandLoader = Callable[[], Iterable["CommandSpec"]]
CommandCheck = Callable[[], bool]


def _normalize_command_name(value: str) -> str:
    return str(value or "").strip().lstrip("/").lower()


def _parse_text_command(text: str) -> tuple[str, dict[str, Any]]:
    """Split ``/name (MCP) <args>`` style input into name + args dict.

    F-038: Claude's slash parser (``utils/slashCommandParsing.ts:45-50``)
    treats the canonical command name for MCP-routed commands as
    ``"<name> (MCP)"`` (the marker is *part of the name* used for
    matching / telemetry / canonicalization). The args start after the
    marker. The pre-fix Viola implementation stripped the marker and
    set ``args["is_mcp"] = True``, which dropped the canonical suffix
    every caller relying on the Claude shape needed to see.
    """

    stripped = str(text or "").strip()
    if not stripped or not stripped.startswith("/"):
        return "", {}
    command_text = stripped[1:]
    name, _, rest = command_text.partition(" ")
    args: dict[str, Any] = {}
    rest = rest.strip()
    if rest.lower().startswith("(mcp)"):
        # Carry both: the Python-friendly is_mcp flag AND the canonical
        # name suffix Claude downstream consumers expect.
        args["is_mcp"] = True
        args["canonical_name"] = "%s (MCP)" % name
        rest = rest[5:].strip()
    if rest:
        args["raw_args"] = rest
    return name, args


@dataclass(frozen=True)
class CommandSpec:
    """Registered command metadata.

    Availability checks intentionally run at resolve time so account/session
    changes take effect without rebuilding the registry.

    S9-02: Claude exposes additional command metadata that the registry
    must round-trip:

    * ``command_type`` — ``"prompt"`` (rewrites the user turn into a prompt),
      ``"local"`` (handler runs locally without LLM), or ``"local-jsx"``
      (handler produces UI). Defaults to ``"local"``.
    * ``progress_message`` — text shown while a slow command is running.
    * ``user_facing`` — when False, the command is excluded from ``/help``.
    * ``argument_hint`` — human-readable hint shown in command pickers.
    * ``is_session_control`` — flag marking commands that mutate global
      session state (used by audit/lifecycle hooks). The pipeline uses
      this to require permission re-prompts.
    """

    name: str
    aliases: tuple[str, ...] = ()
    source: str = "builtin"
    handler: CommandHandler | None = None
    requires_permission: bool = False
    output_style: str | None = None
    remote_safe: bool = False
    availability: CommandCheck | None = None
    enabled: bool | CommandCheck = True
    description: str | None = None
    command_type: str = "local"
    progress_message: str | None = None
    user_facing: bool = True
    argument_hint: str | None = None
    is_session_control: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_name = _normalize_command_name(self.name)
        if not normalized_name:
            raise ValueError("Command name is required")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(
            self,
            "aliases",
            tuple(alias.strip() for alias in self.aliases if str(alias or "").strip()),
        )
        normalized_command_type = str(self.command_type or "local").strip().lower()
        if normalized_command_type not in {"local", "prompt", "local-jsx"}:
            normalized_command_type = "local"
        object.__setattr__(self, "command_type", normalized_command_type)

    def is_enabled(self) -> bool:
        if callable(self.enabled):
            return bool(self.enabled())
        return bool(self.enabled)

    def is_available(self) -> bool:
        if self.availability is None:
            return True
        return bool(self.availability())


@dataclass(frozen=True)
class CommandInvocation:
    """Resolved command invocation tied back to the canonical frame chain."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    source_frame_uuid: str | None = None
    session_id: str | None = None
    source: str = "local"
    raw_input: str | None = None
    output_style: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalize_command_name(self.name))
        object.__setattr__(self, "args", dict(self.args or {}))


class CommandRegistry:
    """Single registry for command/session-control parity surfaces."""

    def __init__(self, loaders: Iterable[CommandLoader] | None = None) -> None:
        self._specs: dict[str, CommandSpec] = {}
        self._aliases: dict[str, str] = {}
        self._loaders: list[CommandLoader] = list(loaders or ())
        self._loaded = False
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic counter bumped when command caches are invalidated."""

        return self._generation

    def register(self, spec: CommandSpec) -> None:
        """Register a command spec and its aliases."""

        self._register(spec, replace=False)

    def add_loader(self, loader: CommandLoader) -> None:
        """Add a memoized expensive command source."""

        self._loaders.append(loader)
        self.invalidate_cache()

    def invalidate_cache(self) -> None:
        """Invalidate memoized loader output without dropping manual specs."""

        self._loaded = False
        self._generation += 1

    def resolve(
        self,
        text_or_event: str | Mapping[str, Any],
        *,
        source_frame_uuid: str | None = None,
        session_id: str | None = None,
        remote: bool = False,
    ) -> CommandInvocation | None:
        """Resolve slash-like or voice-equivalent command input."""

        self._ensure_loaded()
        name, args, raw_input, is_remote, resolved_source_frame_uuid, resolved_session_id = self._parse_input(
            text_or_event,
            source_frame_uuid=source_frame_uuid,
            session_id=session_id,
            remote=remote,
        )
        lookup_name = args.get("canonical_name") if isinstance(args.get("canonical_name"), str) else name
        canonical_name = self._aliases.get(_normalize_command_name(lookup_name), _normalize_command_name(lookup_name))
        spec = self._specs.get(canonical_name)
        if spec is None or not self._spec_is_usable(spec, remote=is_remote):
            return None
        return CommandInvocation(
            name=spec.name,
            args=args,
            source_frame_uuid=resolved_source_frame_uuid,
            session_id=resolved_session_id,
            source="remote" if is_remote else spec.source,
            raw_input=raw_input,
            output_style=spec.output_style,
        )

    def match(
        self,
        text_or_event: str | Mapping[str, Any],
        *,
        source_frame_uuid: str | None = None,
        session_id: str | None = None,
        remote: bool = False,
    ) -> CommandInvocation | None:
        """Alias for call sites mirroring Claude Code command matching."""

        return self.resolve(
            text_or_event,
            source_frame_uuid=source_frame_uuid,
            session_id=session_id,
            remote=remote,
        )

    async def invoke(self, invocation: CommandInvocation) -> Any:
        """Invoke a resolved command handler."""

        self._ensure_loaded()
        spec = self._specs.get(invocation.name)
        if spec is None:
            raise KeyError("Unknown command: %s" % invocation.name)
        if spec.handler is None:
            return None
        result = spec.handler(invocation)
        if inspect.isawaitable(result):
            return await result
        return result

    def get_spec(self, name: str) -> CommandSpec | None:
        """Return the registered spec for a resolved command name, if any.

        Public accessor over the internal spec map so callers (e.g. the intent
        pipeline resolving a command's ``command_type``) do not reach into the
        private ``_specs`` dict.
        """

        return self._specs.get(name)

    def list_available(self, *, remote_safe_only: bool = False) -> list[CommandSpec]:
        """Return currently usable commands, re-checking availability each call."""

        self._ensure_loaded()
        return [
            spec
            for spec in self._specs.values()
            if spec.user_facing and self._spec_is_usable(spec, remote=remote_safe_only)
        ]

    def build_invocation_frame(self, invocation: CommandInvocation) -> Frame:
        """Convert a command invocation to canonical model-visible meta state."""

        self._ensure_loaded()
        spec = self._specs.get(invocation.name)
        args_text = json.dumps(invocation.args, sort_keys=True)
        source = spec.source if spec is not None else invocation.source
        command_type = spec.command_type if spec is not None else "local"
        allowed_tools = spec.extra.get("allowed_tools") if spec is not None else None
        text = (
            "<command-message>\n"
            "<command-name>/%s</command-name>\n"
            "<command-source>%s</command-source>\n"
            "<command-type>%s</command-type>\n"
            "<command-args>%s</command-args>\n"
            "</command-message>"
        ) % (invocation.name, source, command_type, args_text)
        if allowed_tools:
            text += "\n<command-permissions>%s</command-permissions>" % json.dumps(allowed_tools, sort_keys=True)
        extra = {
            "command_name": invocation.name,
            "command_source": source,
            "command_type": command_type,
            "args": invocation.args,
            "requires_permission": bool(spec.requires_permission) if spec is not None else False,
            "output_style": invocation.output_style,
            "source_frame_uuid": invocation.source_frame_uuid,
            "allowed_tools": allowed_tools,
        }
        return Frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            blocks=(SystemReminderBlock(text=text, source_tag="command"),),
            is_meta=True,
            origin="command",
            session_id=invocation.session_id,
            extra=extra,
        )

    def _register(self, spec: CommandSpec, *, replace: bool) -> None:
        normalized = _normalize_command_name(spec.name)
        if not replace and normalized in self._specs:
            raise ValueError("Duplicate command: %s" % spec.name)
        self._specs[normalized] = spec
        for alias in spec.aliases:
            normalized_alias = _normalize_command_name(alias)
            if not normalized_alias:
                continue
            if not replace and normalized_alias in self._aliases and self._aliases[normalized_alias] != normalized:
                raise ValueError("Duplicate command alias: %s" % alias)
            self._aliases[normalized_alias] = normalized

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        for loader in self._loaders:
            for spec in loader():
                self._register(spec, replace=True)
        self._loaded = True

    def _parse_input(
        self,
        text_or_event: str | Mapping[str, Any],
        *,
        source_frame_uuid: str | None,
        session_id: str | None,
        remote: bool,
    ) -> tuple[str, dict[str, Any], str | None, bool, str | None, str | None]:
        if isinstance(text_or_event, Mapping):
            name = str(text_or_event.get("name") or text_or_event.get("command") or "")
            args_value = text_or_event.get("args", {})
            if not isinstance(args_value, Mapping):
                args_value = {"value": args_value}
            if source_frame_uuid is None:
                source_frame_uuid = _string_or_none(text_or_event.get("source_frame_uuid"))
            if session_id is None:
                session_id = _string_or_none(text_or_event.get("session_id"))
            raw_input = str(text_or_event.get("text") or name)
            is_remote = remote or str(text_or_event.get("source") or "").lower() == "remote"
            return name, dict(args_value), raw_input, is_remote, source_frame_uuid, session_id

        name, args = _parse_text_command(text_or_event)
        return name, args, str(text_or_event), remote, source_frame_uuid, session_id

    def _spec_is_usable(self, spec: CommandSpec, *, remote: bool) -> bool:
        if remote and not spec.remote_safe:
            return False
        return spec.is_enabled() and spec.is_available()


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "CommandInvocation",
    "CommandRegistry",
    "CommandSpec",
]
