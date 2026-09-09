"""Dynamic output-style prompt overlay frames."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
)

STYLE_SAFETY_FLOOR = (
    "Output style only controls presentation. It does not override safety, "
    "permissions, tool policy, gate state, or user/account boundaries."
)
DEFAULT_OUTPUT_STYLE_NAME = "default"
OUTPUT_STYLE_SETTING_KEY = "outputStyle"
OUTPUT_STYLE_SETTING_ALIASES = (OUTPUT_STYLE_SETTING_KEY, "output_style")

# F-040: Port Claude's canonical built-in output style prompts verbatim
# (constants/outputStyles.ts:39-135). Pre-fix the Viola variants were short
# custom summaries that omitted Claude's request-format scaffolding,
# TODO(human) workflow, and Insights block — overlapping by name only.
# Keeping these 1:1 means a user switching between Claude and Viola sees
# the same in-session behavior under the same style name.

_EXPLANATORY_FEATURE_PROMPT = (
    "\n## Insights\n"
    "In order to encourage learning, before and after writing code, always provide brief "
    "educational explanations about implementation choices using (with backticks):\n"
    '"`★ Insight ─────────────────────────────────────`\n'
    "[2-3 key educational points]\n"
    '`─────────────────────────────────────────────────`"\n'
    "\n"
    "These insights should be included in the conversation, not in the codebase. You "
    "should generally focus on interesting insights that are specific to the codebase "
    "or the code you just wrote, rather than general programming concepts."
)

_BUILTIN_OUTPUT_STYLES: dict[str, str] = {
    "Explanatory": (
        "You are an interactive CLI tool that helps users with software engineering tasks. "
        "In addition to software engineering tasks, you should provide educational insights "
        "about the codebase along the way.\n"
        "\n"
        "You should be clear and educational, providing helpful explanations while remaining "
        "focused on the task. Balance educational content with task completion. When providing "
        "insights, you may exceed typical length constraints, but remain focused and relevant.\n"
        "\n"
        "# Explanatory Style Active\n" + _EXPLANATORY_FEATURE_PROMPT
    ),
    "Learning": (
        "You are an interactive CLI tool that helps users with software engineering tasks. "
        "In addition to software engineering tasks, you should help users learn more about "
        "the codebase through hands-on practice and educational insights.\n"
        "\n"
        "You should be collaborative and encouraging. Balance task completion with learning "
        "by requesting user input for meaningful design decisions while handling routine "
        "implementation yourself.\n"
        "\n"
        "# Learning Style Active\n"
        "## Requesting Human Contributions\n"
        "In order to encourage learning, ask the human to contribute 2-10 line code pieces "
        "when generating 20+ lines involving:\n"
        "- Design decisions (error handling, data structures)\n"
        "- Business logic with multiple valid approaches\n"
        "- Key algorithms or interface definitions\n"
        "\n"
        "**TodoList Integration**: If using a TodoList for the overall task, include a "
        'specific todo item like "Request human input on [specific decision]" when planning '
        "to request human input. This ensures proper task tracking. Note: TodoList is not "
        "required for all tasks.\n"
        "\n"
        "Example TodoList flow:\n"
        '   ✓ "Set up component structure with placeholder for logic"\n'
        '   ✓ "Request human collaboration on decision logic implementation"\n'
        '   ✓ "Integrate contribution and complete feature"\n'
        "\n"
        "### Request Format\n"
        "```\n"
        "● **Learn by Doing**\n"
        "**Context:** [what's built and why this decision matters]\n"
        "**Your Task:** [specific function/section in file, mention file and TODO(human) but "
        "do not include line numbers]\n"
        "**Guidance:** [trade-offs and constraints to consider]\n"
        "```\n"
        "\n"
        "### Key Guidelines\n"
        "- Frame contributions as valuable design decisions, not busy work\n"
        "- You must first add a TODO(human) section into the codebase with your editing "
        "tools before making the Learn by Doing request\n"
        "- Make sure there is one and only one TODO(human) section in the code\n"
        "- Don't take any action or output anything after the Learn by Doing request. Wait "
        "for human implementation before proceeding.\n"
        "\n"
        "### Example Requests\n"
        "\n"
        "**Whole Function Example:**\n"
        "```\n"
        "â— **Learn by Doing**\n"
        "\n"
        "**Context:** I've set up the hint feature UI with a button that triggers the hint "
        "system. The infrastructure is ready: when clicked, it calls selectHintCell() to "
        "determine which cell to hint, then highlights that cell with a yellow background "
        "and shows possible values. The hint system needs to decide which empty cell would "
        "be most helpful to reveal to the user.\n"
        "\n"
        "**Your Task:** In sudoku.js, implement the selectHintCell(board) function. Look "
        "for TODO(human). This function should analyze the board and return {row, col} "
        "for the best cell to hint, or null if the puzzle is complete.\n"
        "\n"
        "**Guidance:** Consider multiple strategies: prioritize cells with only one "
        "possible value (naked singles), or cells that appear in rows/columns/boxes with "
        "many filled cells. You could also consider a balanced approach that helps without "
        "making it too easy. The board parameter is a 9x9 array where 0 represents empty "
        "cells.\n"
        "```\n"
        "\n"
        "**Partial Function Example:**\n"
        "```\n"
        "â— **Learn by Doing**\n"
        "\n"
        "**Context:** I've built a file upload component that validates files before "
        "accepting them. The main validation logic is complete, but it needs specific "
        "handling for different file type categories in the switch statement.\n"
        "\n"
        "**Your Task:** In upload.js, inside the validateFile() function's switch "
        "statement, implement the 'case \"document\":' branch. Look for TODO(human). "
        "This should validate document files (pdf, doc, docx).\n"
        "\n"
        "**Guidance:** Consider checking file size limits (maybe 10MB for documents?), "
        "validating the file extension matches the MIME type, and returning {valid: "
        "boolean, error?: string}. The file object has properties: name, size, type.\n"
        "```\n"
        "\n"
        "**Debugging Example:**\n"
        "```\n"
        "â— **Learn by Doing**\n"
        "\n"
        "**Context:** The user reported that number inputs aren't working correctly in "
        "the calculator. I've identified the handleInput() function as the likely source, "
        "but need to understand what values are being processed.\n"
        "\n"
        "**Your Task:** In calculator.js, inside the handleInput() function, add 2-3 "
        "console.log statements after the TODO(human) comment to help debug why number "
        "inputs fail.\n"
        "\n"
        "**Guidance:** Consider logging: the raw input value, the parsed result, and any "
        "validation state. This will help us understand where the conversion breaks.\n"
        "```\n"
        "\n"
        "### After Contributions\n"
        "Share one insight connecting their code to broader patterns or system effects. "
        "Avoid praise or repetition.\n" + _EXPLANATORY_FEATURE_PROMPT
    ),
}


@dataclass(frozen=True)
class OutputStyle:
    """Structured prompt overlay selected for one request/session."""

    name: str
    instructions: str
    source: str
    enabled: bool = True
    keep_coding_instructions: bool = True
    description: str | None = None
    priority: int = 0
    # S9-04: plugins can force a style to be active. Multiple plugin forces
    # collapse to the first registered (matches Claude's
    # ``getOutputStyleConfig`` precedence in constants/outputStyles.ts).
    force_for_plugin: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_name = str(self.name or "").strip()
        if not normalized_name:
            raise ValueError("Output style name is required")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "instructions", str(self.instructions or "").strip())
        object.__setattr__(self, "source", str(self.source or "unknown").strip() or "unknown")


def build_output_style_frame(style: OutputStyle) -> Frame:
    """Build a canonical meta frame for an active output style."""

    if not style.enabled:
        raise ValueError("Cannot build frame for disabled output style: %s" % style.name)
    text = ("Output style active: %s\n" "source: %s\n\n" "Instructions:\n%s\n\n" "%s") % (
        style.name,
        style.source,
        style.instructions,
        STYLE_SAFETY_FLOOR,
    )
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag="output-style"),),
        is_meta=True,
        origin="output_style",
        extra={
            "style_name": style.name,
            "style_source": style.source,
            "keep_coding_instructions": style.keep_coding_instructions,
            "description": style.description,
            "priority": style.priority,
            "force_for_plugin": style.force_for_plugin,
            **style.extra,
        },
    )


def build_output_style_section(style: OutputStyle) -> Frame:
    """Build a Claude-shaped ``# Output Style`` system-prompt section.

    S9-03: Claude renders the active output style as a top-level
    ``# Output Style: <name>\\n<prompt>`` section under the main system
    prompt (see ``src/constants/prompts.ts:152-158, 505-506``). Returning
    this as a SYSTEM frame (not META_USER) lets it live alongside
    ``VIOLA_UNIFIED_PROMPT`` instead of being relegated to a runtime
    reminder.
    """

    if not style.enabled:
        raise ValueError("Cannot build section for disabled output style: %s" % style.name)
    metadata = _style_metadata_lines(style)
    metadata_text = "\n".join(metadata)
    metadata_block = ("\n\n# Output Style Metadata\n%s" % metadata_text) if metadata_text else ""
    text = "# Output Style: %s\n%s%s\n\n%s" % (
        style.name,
        style.instructions,
        metadata_block,
        STYLE_SAFETY_FLOOR,
    )
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.SYSTEM,
        blocks=(SystemReminderBlock(text=text, source_tag="output-style"),),
        is_meta=False,
        origin="output_style",
        extra={
            "style_name": style.name,
            "style_source": style.source,
            "keep_coding_instructions": style.keep_coding_instructions,
            "description": style.description,
            "priority": style.priority,
            "force_for_plugin": style.force_for_plugin,
            **style.extra,
        },
    )


class OutputStyleRegistry:
    """Small precedence-aware registry for output style overlays."""

    def __init__(self, styles: list[OutputStyle] | None = None) -> None:
        self._styles: dict[str, OutputStyle] = {}
        for style in styles or []:
            self.register(style)

    def register(self, style: OutputStyle) -> None:
        self._styles[style.name.lower()] = style

    def resolve(self, name: str | None = None) -> OutputStyle | None:
        enabled = [style for style in self._styles.values() if style.enabled]
        # S9-04: plugin-forced styles take precedence over the named lookup
        # AND the priority sort. Claude logs (but does not block) when
        # multiple plugins force a style and arbitrarily picks the first.
        forced = [style for style in enabled if style.force_for_plugin and style.source == "plugin"]
        if forced:
            return forced[0]
        if name:
            style = self._styles.get(name.lower())
            return style if style is not None and style.enabled else None
        if not enabled:
            return None
        return sorted(enabled, key=lambda style: style.priority, reverse=True)[0]

    def list_enabled(self) -> list[OutputStyle]:
        return [style for style in self._styles.values() if style.enabled]

    def list_forced(self) -> list[OutputStyle]:
        """Return all plugin-forced styles (S9-04 diagnostic helper)."""

        return [
            style
            for style in self._styles.values()
            if style.enabled and style.force_for_plugin and style.source == "plugin"
        ]


def builtin_output_style_registry() -> OutputStyleRegistry:
    """Return the built-in non-default output styles."""

    registry = OutputStyleRegistry()
    for name, instructions in _BUILTIN_OUTPUT_STYLES.items():
        registry.register(
            OutputStyle(
                name=name,
                instructions=instructions,
                source="built-in",
            )
        )
    return registry


def load_output_styles_dir(path: str | Path, *, source: str) -> list[OutputStyle]:
    """Load simple markdown output styles from a directory.

    Front matter is intentionally minimal: ``name``, ``description``,
    ``enabled``, ``priority`` and (for plugins) ``forceForPlugin`` are
    recognized when present.
    """

    root = Path(path)
    if not root.exists():
        return []
    styles: list[OutputStyle] = []
    for file_path in sorted(root.glob("*.md")):
        text = file_path.read_text(encoding="utf-8")
        metadata, instructions = _split_front_matter(text)
        enabled = str(metadata.get("enabled", "true")).lower() not in {"0", "false", "no"}
        priority_text = metadata.get("priority", "0")
        try:
            priority = int(priority_text)
        except ValueError:
            priority = 0
        force_for_plugin = str(metadata.get("forceForPlugin", metadata.get("force_for_plugin", "false"))).lower() in {
            "1",
            "true",
            "yes",
        }
        keep_coding_instructions = _metadata_bool(
            metadata,
            "keep-coding-instructions",
            "keepCodingInstructions",
            "keep_coding_instructions",
            default=True,
        )
        styles.append(
            OutputStyle(
                name=metadata.get("name", file_path.stem),
                description=metadata.get("description"),
                instructions=instructions,
                source=source,
                enabled=enabled,
                priority=priority,
                force_for_plugin=force_for_plugin,
                keep_coding_instructions=keep_coding_instructions,
                extra=_style_file_extra(file_path, source=source),
            )
        )
    return styles


def load_plugin_output_styles(plugin_roots: Iterable[str | Path] | None = None) -> list[OutputStyle]:
    """Load plugin-bundled output styles with plugin/cache metadata."""

    roots = list(plugin_roots or _default_plugin_roots())
    styles: list[OutputStyle] = []
    for root_value in roots:
        root = Path(root_value)
        if not root.exists():
            continue
        for plugin_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            style_dir = plugin_dir / "output-styles"
            for style in load_output_styles_dir(style_dir, source="plugin"):
                styles.append(
                    replace(
                        style,
                        extra={
                            **style.extra,
                            "plugin_id": plugin_dir.name,
                            "cache_namespace": "plugin-output-style",
                        },
                    )
                )
    return styles


def load_output_style_registry(
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
    plugin_styles: list[OutputStyle] | None = None,
    plugin_roots: Iterable[str | Path] | None = None,
) -> OutputStyleRegistry:
    """Load built-in + custom + plugin-forced output styles for active lookup.

    Precedence mirrors Claude's ``getAllOutputStyles`` priority order:
    built-in → plugin → user → project → policy. The caller supplies
    ``plugin_styles`` (which can include ``force_for_plugin=True``); a
    forced plugin style wins the ``resolve`` race regardless of the
    settings-stored selection.

    F-041 decision: Viola intentionally loads BOTH ``.viola`` and
    ``.claude`` roots at the user and project layers. ``.viola`` is the
    canonical Viola extension root for Viola-specific styles; ``.claude``
    is loaded as a Claude-compat layer so users migrating from Claude
    Code don't have to rewrite their styles. The load order in
    ``style_dirs`` below is the precedence — last write wins per name
    inside the registry, so a same-named ``.claude`` style overrides a
    ``.viola`` style at the same scope. Documented precedence:

        built-in → plugin →
        home/.viola → home/.claude (Claude override at user scope) →
        project/.viola → project/.claude (Claude override at project scope) →
        project/.viola/policy (policy hardens last)
    """

    registry = builtin_output_style_registry()
    project_root = Path(cwd) if cwd is not None else Path.cwd()
    home_root = Path(home) if home is not None else Path.home()
    # Plugin layer first (lowest non-builtin) so user/project files can
    # override description fields if name collides.
    for style in (*load_plugin_output_styles(plugin_roots), *(plugin_styles or [])):
        registry.register(style)
    style_dirs = (
        (home_root / ".viola" / "output-styles", "userSettings"),
        (home_root / ".claude" / "output-styles", "userSettings"),
        (project_root / ".viola" / "output-styles", "projectSettings"),
        (project_root / ".claude" / "output-styles", "projectSettings"),
        (project_root / ".viola" / "policy" / "output-styles", "policySettings"),
    )
    for style_dir, source in style_dirs:
        for style in load_output_styles_dir(style_dir, source=source):
            registry.register(style)
    return registry


def _style_file_extra(file_path: Path, *, source: str) -> dict[str, Any]:
    try:
        mtime_ns = file_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return {
        "style_path": str(file_path),
        "cache_key": "%s:%s:%s" % (source, file_path.as_posix(), mtime_ns),
    }


def _style_metadata_lines(style: OutputStyle) -> list[str]:
    lines = ["source: %s" % style.source]
    plugin_id = style.extra.get("plugin_id")
    if plugin_id:
        lines.append("plugin_id: %s" % plugin_id)
    cache_key = style.extra.get("cache_key")
    if cache_key:
        lines.append("cache_key: %s" % cache_key)
    if style.force_for_plugin:
        lines.append("force_for_plugin: true")
    return lines


def _default_plugin_roots() -> tuple[Path, ...]:
    try:
        from core.constants import PLUGIN_BUILTIN_DIR, PLUGIN_USER_DIR
    except ImportError:
        return ()
    return (Path(PLUGIN_BUILTIN_DIR), Path(PLUGIN_USER_DIR))


def resolve_output_style(
    value: object,
    *,
    registry: OutputStyleRegistry | None = None,
) -> OutputStyle | None:
    """Resolve a SettingsManager output-style value to a frame-ready style."""

    if isinstance(value, Mapping):
        name = str(value.get("name") or value.get("style") or "").strip()
        instructions = str(value.get("prompt") or value.get("instructions") or "").strip()
        if not name or not instructions:
            return None
        enabled_value = value.get("enabled", True)
        return OutputStyle(
            name=name,
            instructions=instructions,
            source=str(value.get("source") or "settings"),
            enabled=bool(enabled_value),
            keep_coding_instructions=_metadata_bool(
                value,
                "keep-coding-instructions",
                "keepCodingInstructions",
                "keep_coding_instructions",
                default=True,
            ),
            description=str(value.get("description") or "") or None,
        )

    if not isinstance(value, str):
        return None
    selected = value.strip()
    if not selected or selected.lower() in {DEFAULT_OUTPUT_STYLE_NAME, "normal", "none"}:
        return None
    active_registry = registry or builtin_output_style_registry()
    return active_registry.resolve(selected)


def get_active_output_style(
    settings_manager: Any,
    *,
    registry: OutputStyleRegistry | None = None,
) -> OutputStyle | None:
    """Read the active output style from SettingsManager-compatible storage."""

    for key in OUTPUT_STYLE_SETTING_ALIASES:
        value = settings_manager.get(key, None)
        style = resolve_output_style(value, registry=registry)
        if style is not None:
            return style
    return None


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text.strip()
    _, _, remainder = text.partition("\n")
    front_matter, separator, body = remainder.partition("\n---")
    if not separator:
        return {}, text.strip()
    metadata: dict[str, str] = {}
    for line in front_matter.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, body.lstrip("\r\n").strip()


def _metadata_bool(metadata: Mapping[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return default


__all__ = [
    "DEFAULT_OUTPUT_STYLE_NAME",
    "OUTPUT_STYLE_SETTING_ALIASES",
    "OUTPUT_STYLE_SETTING_KEY",
    "STYLE_SAFETY_FLOOR",
    "OutputStyle",
    "OutputStyleRegistry",
    "build_output_style_frame",
    "build_output_style_section",
    "builtin_output_style_registry",
    "get_active_output_style",
    "load_output_style_registry",
    "load_output_styles_dir",
    "load_plugin_output_styles",
    "resolve_output_style",
]
