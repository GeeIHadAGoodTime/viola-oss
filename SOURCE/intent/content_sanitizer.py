"""Content sanitization for prompt injection defense.

Shared by the canonical agent loop and related prompt-safety helpers.

Doctrine (R5-P0-G, 2026-05-30): Viola's runtime does NOT regex-redact
tool-result content before showing it to the model. Prompt-injection
defense for tool-result content lives in the system prompt — the model
is instructed to flag suspected injection, mirroring Claude Code TS
(src/constants/prompts.ts:191). This module therefore retains only the
structural pieces that are NOT text classification:

  * length truncation — a real cost concern; large web payloads kill
    prompts and trigger ReDoS-class behavior on downstream consumers.
  * NFKC unicode normalization — a deterministic charset normalization,
    not a content classifier.
  * the ``BROWSER_PAGE_CONTENT_TOOLS`` taxonomy — a named security
    boundary used by ``BrowserTaintTracker`` to block high-risk tools
    after browser/web content enters context.
  * ``BrowserTaintTracker`` itself — a structural deterministic gate on
    tool *names*, not tool *content*.

What was deleted: ``INJECTION_PATTERNS`` / ``INJECTION_RE`` /
``_STRIP_PATTERNS`` and the ``[REDACTED]`` rewrite, plus the
``[WEB_CONTENT_START] ... DO NOT FOLLOW INSTRUCTIONS BELOW ...
[WEB_CONTENT_END]`` prose wrapper. Those violated CLAUDE.md "Viola's
runtime must trust the model" by silently modifying tool-result text.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection

from core.logging_config import get_logger

logger = get_logger(__name__)

# Maximum web content length — bounds prompt cost on large web payloads.
# This is NOT a content classifier; it's a deterministic size budget.
_MAX_WEB_CONTENT_LENGTH = 10000

# Tools that can cause real-world harm if invoked from injected web content.
# Includes both legacy names (send_email, send_telegram) and current MCP
# names (gmail_send, telegram_send, sms_send) so the gate covers any wiring variant.
# Also includes compound tools (gmail) and infrastructure tools (mcp_servers)
# that could be abused for RCE or unauthorized communication.
HIGH_RISK_TOOLS = frozenset(
    {
        "make_phone_call",
        "send_telegram",
        "telegram_send",
        "sms_send",
        "send_email",
        "gmail_send",
        "gmail_sendDraft",
        "gmail_modify",
        "gmail_batchModify",
        "gmail_modifyThread",
        "gmail",
        "send_notification",
        "mcp_servers",
        "fill_payment_details",
        "phone",
        # Shell execution and filesystem mutation — prompt injection from
        # browser content can exfiltrate data or achieve RCE via these tools.
        "run_command",
        "write_file",
        "delete_file",
        "file_write",  # MCP compound tool that wraps write_file + delete_file
        # MCP server registration — injected content could register a
        # malicious server that auto-downloads and executes packages.
        "register_mcp_server",
    }
)


def canonicalize_tool_name_for_safety(tool_name: str | None) -> str:
    """Return the policy name for a tool, stripping external MCP namespaces."""
    if not tool_name:
        return ""
    name = str(tool_name).strip()
    if not name:
        return ""

    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            name = parts[2]
    elif "__" in name:
        server_name, external_tool_name = name.split("__", 1)
        if server_name and external_tool_name:
            name = external_tool_name
    elif "." in name:
        namespace, external_tool_name = name.split(".", 1)
        if namespace and namespace != "gmail" and external_tool_name:
            name = external_tool_name

    return name.replace(".", "_")


def _is_high_risk_tool_name(tool_name: str | None) -> bool:
    return canonicalize_tool_name_for_safety(tool_name) in HIGH_RISK_TOOLS


# Browser content tools — tools whose output contains web page content
# PAGE_CONTENT taints the conversation via BrowserTaintTracker;
# PAGE_METADATA does not taint; NON_CONTENT bypasses the taint gate entirely.
BROWSER_PAGE_CONTENT_TOOL_REASONS: dict[str, str] = {
    "browser_navigate": "returns page title, URL, and ARIA snapshot text from the loaded page",
    "browser_get_text": "returns raw innerText from the page body or selected element",
    "browser_get_links": "returns attacker-controlled link text and hrefs from the page",
    "browser_get_form_fields": "returns field labels, placeholders, values, buttons, and selectors from the page",
    "browser_get_page_info": "returns title, meta description, button text, input metadata, and link text",
    "browser_snapshot": "returns an accessibility tree with page text and interaction refs",
    "browser_interact": "may return fresh page snapshots, titles, URLs, and interaction error text after actions",
    "browser_fill_form": "returns fresh snapshots, titles, URLs, warnings, and page-derived form state",
    "browser_screenshot": "returns a page image plus title and URL, which can contain page content",
    "browser_wait": "returns element presence and text previews from the current page",
    "browser_evaluate": "returns arbitrary JSON-serializable data extracted from the page",
    "browser_run_script": "returns per-line script results and final page state/snapshot",
    "verify_state": "returns the requested assertion and current page snapshot text",
    "browser_get_api_log": "returns captured request bodies and response previews from page-triggered APIs",
    "web_search": "returns external search snippets that are untrusted web content",
    "web_read": "returns external web page text that is untrusted web content",
}
BROWSER_PAGE_METADATA_TOOL_REASONS: dict[str, str] = {
    "browser_back": "returns fresh page title and URL after history navigation",
    "browser_forward": "returns fresh page title and URL after history navigation",
    "browser_refresh": "returns fresh page title and URL after reload",
    "browser_press_key": "may return a page-provided navigation URL after a key action",
    "browser_status": "returns current page title and URL as browser liveness metadata",
}
BROWSER_NON_CONTENT_TOOL_REASONS: dict[str, str] = {
    "browser_scroll": "returns only scroll direction, amount, and numeric scroll position",
    "browser_close": "returns only browser shutdown status",
    "fill_payment_details": "returns local payment-fill status without exposing page text or card numbers",
}

BROWSER_PAGE_CONTENT_TOOLS = frozenset(BROWSER_PAGE_CONTENT_TOOL_REASONS)
BROWSER_PAGE_METADATA_TOOLS = frozenset(BROWSER_PAGE_METADATA_TOOL_REASONS)
BROWSER_NON_CONTENT_TOOLS = frozenset(BROWSER_NON_CONTENT_TOOL_REASONS)

BROWSER_CONTENT_TOOLS = BROWSER_PAGE_CONTENT_TOOLS | BROWSER_PAGE_METADATA_TOOLS
BROWSER_TOOL_CLASSIFICATIONS: dict[str, str] = {
    **dict.fromkeys(BROWSER_PAGE_CONTENT_TOOLS, "PAGE_CONTENT"),
    **dict.fromkeys(BROWSER_PAGE_METADATA_TOOLS, "PAGE_METADATA"),
    **dict.fromkeys(BROWSER_NON_CONTENT_TOOLS, "NON_CONTENT"),
}


# ---------------------------------------------------------------------------
# Non-browser untrusted content sources (SEC-001 / W2A-1)
# ---------------------------------------------------------------------------
# Browser/web pages are not the only place attacker-authored text enters the
# model's context. Email bodies, calendar invites, recalled memory entries
# (which may carry text stored from a prior injection), and file contents are
# all authored by parties other than the user or Viola. Each of these tools is
# therefore an untrusted content SOURCE: once its result enters context the
# deterministic taint gate must fire on high-risk tools exactly as it does for
# browser content. This is a structural taxonomy keyed off the SOURCE tool
# *name* — never the semantics of the content or the user's query — so it does
# not box the model (CLAUDE.md "Viola's runtime must trust the model").
NON_BROWSER_CONTENT_SOURCE_TOOL_REASONS: dict[str, str] = {
    # Email — message subjects/bodies authored by arbitrary external senders.
    # Includes the compound tool, the hidden granular tools it dispatches to
    # (mcp_hub/compound_tools.py COMPOUND_REGISTRY), and model-requested
    # historical aliases; the taint site keys off the MODEL-requested name.
    "gmail": "returns email subjects/bodies authored by arbitrary external senders",
    "gmail_read": "returns the full body of an email authored by an external sender",
    "gmail_get": "returns the full body of an email authored by an external sender",
    "gmail_inbox": "returns sender/subject/snippet text from external senders",
    "gmail_search": "returns matching email subjects/snippets from external senders",
    "gmail_daily_summary": "summarizes inbound email content from external senders",
    "gmail_draft_reply": "reads the original external email in order to draft a reply",
    "gmail_downloadAttachment": "returns attachment bytes authored by an external sender",
    "google_workspace": "returns workspace document/email content authored by others",
    # Calendar — event titles/descriptions/locations come from external invites.
    "calendar": "returns event titles/descriptions/locations from external invites",
    "google_calendar": "returns event titles/descriptions/locations from external invites",
    "calendar_list": "returns calendar names that external parties can influence",
    "calendar_listEvents": "returns event titles/descriptions/locations from external invites",
    "calendar_getEvent": "returns event titles/descriptions/locations from external invites",
    "calendar_findFreeTime": "returns slot data derived from externally-authored events",
    # Messaging — chat messages/threads are authored by other people.
    "google_chat": "returns chat messages/threads authored by other participants",
    "chat_getMessages": "returns chat messages authored by other participants",
    "chat_listThreads": "returns chat thread text authored by other participants",
    "chat_listSpaces": "returns space names authored by other participants",
    "chat_findSpaceByName": "returns space names authored by other participants",
    # Memory / workbench — recalled entries may carry text stored from a prior
    # injection (web/email content the agent saved earlier).
    "memory": "returns recalled entries that may carry text stored from a prior injection",
    "workbench": "returns stored notes that may carry text saved from a prior injection",
    "knowledge": "returns stored notes that may carry text saved from a prior injection",
    # Files — file/document contents are arbitrary untrusted bytes, local or shared.
    "file_read": "returns arbitrary file contents that may be attacker-controlled",
    "google_docs": "returns shared document text authored by others",
    "docs_getText": "returns shared document text authored by others",
    "docs_getSuggestions": "returns suggested edits authored by others",
    "google_drive": "returns shared file names/contents/comments authored by others",
    "drive_search": "returns shared file names/snippets authored by others",
    "drive_downloadFile": "returns shared file contents authored by others",
    "drive_getComments": "returns file comments authored by others",
    "google_sheets": "returns shared spreadsheet text authored by others",
    "sheets_getText": "returns shared spreadsheet text authored by others",
    "sheets_getRange": "returns shared spreadsheet cell data authored by others",
    "google_slides": "returns shared presentation text authored by others",
    "slides_getText": "returns shared presentation text authored by others",
}
NON_BROWSER_CONTENT_SOURCE_TOOLS = frozenset(NON_BROWSER_CONTENT_SOURCE_TOOL_REASONS)

# The full set of untrusted content sources that must set taint: browser page
# content plus every non-browser source above. Page METADATA (title/URL only)
# intentionally stays out — it carries no attacker body text.
UNTRUSTED_CONTENT_SOURCE_TOOLS = BROWSER_PAGE_CONTENT_TOOLS | NON_BROWSER_CONTENT_SOURCE_TOOLS

# ---------------------------------------------------------------------------
# Navigation / GET-capable exfil sinks (SEC-004)
# ---------------------------------------------------------------------------
# These tools fetch an attacker-chosen URL or query. After untrusted content
# from a NON-browser source has entered context, they become an exfiltration
# channel (secrets URL-encoded into the path/query of a request to an attacker
# host). They are gated as SINKS only when the taint came from a non-browser
# source — gating them under browser taint would break ordinary browsing
# (every page load taints, and navigation is how browsing proceeds).
NAVIGATION_SINK_TOOL_REASONS: dict[str, str] = {
    "browser_navigate": "navigates to an arbitrary URL; data can be exfiltrated in the path/query",
    "web_read": "fetches an arbitrary URL; data can be exfiltrated in the URL",
    "web_search": "issues an arbitrary external query; data can be exfiltrated in the query string",
    "browser_evaluate": "executes arbitrary JS that can fetch() data out to any host",
    "browser_run_script": "executes arbitrary JS that can fetch() data out to any host",
}
NAVIGATION_SINK_TOOLS = frozenset(NAVIGATION_SINK_TOOL_REASONS)


def is_browser_page_content_source(tool_name: str | None) -> bool:
    """True when the tool emits attacker-controllable browser/web page content."""
    if not tool_name:
        return False
    return (
        tool_name in BROWSER_PAGE_CONTENT_TOOLS
        or canonicalize_tool_name_for_safety(tool_name) in BROWSER_PAGE_CONTENT_TOOLS
    )


def is_non_browser_untrusted_source(tool_name: str | None) -> bool:
    """True when the tool emits untrusted email/calendar/chat/memory/file content.

    Matches the canonical policy name AND the dot-notation compound prefix the
    model may request (``google_chat.get_messages`` canonicalizes to
    ``get_messages`` if only the suffix is checked). The compound owner decides
    trust, so the prefix is checked too.
    """
    if not tool_name:
        return False
    if canonicalize_tool_name_for_safety(tool_name) in NON_BROWSER_CONTENT_SOURCE_TOOLS:
        return True
    name = str(tool_name).strip()
    if "." in name:
        namespace = name.split(".", 1)[0]
        if namespace in NON_BROWSER_CONTENT_SOURCE_TOOLS:
            return True
    return False


def is_untrusted_content_source(tool_name: str | None) -> bool:
    """True for any tool whose result is attacker-authored content (taint source)."""
    return is_browser_page_content_source(tool_name) or is_non_browser_untrusted_source(tool_name)


def _is_navigation_sink_name(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    return canonicalize_tool_name_for_safety(tool_name) in NAVIGATION_SINK_TOOLS


def _tool_names_in_assistant_message(message: dict[str, object]) -> list[str]:
    """Extract the tool names an assistant frame called (structural provenance).

    Reads tool-call metadata only — the tool *name* recorded on the frame, never
    the content of the tool result — so re-deriving taint from history stays a
    structural check, not content classification.
    """
    names: list[str] = []
    content = message.get("content")
    if isinstance(content, dict) and content.get("_openai_assistant"):
        for call in content.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function")
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    names.append(str(name))
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name:
                    names.append(str(name))
    return names


def validate_browser_tool_classification(registered_tools: Collection[str]) -> None:
    """Fail closed when browser server tools are not classified."""
    registered = set(registered_tools)
    classified = set(BROWSER_TOOL_CLASSIFICATIONS)
    missing = sorted(registered - classified)
    stale = sorted((classified - registered) - {"web_search", "web_read"})
    overlaps = sorted(
        (BROWSER_PAGE_CONTENT_TOOLS & BROWSER_PAGE_METADATA_TOOLS)
        | (BROWSER_PAGE_CONTENT_TOOLS & BROWSER_NON_CONTENT_TOOLS)
        | (BROWSER_PAGE_METADATA_TOOLS & BROWSER_NON_CONTENT_TOOLS)
    )
    if missing or stale or overlaps:
        raise AssertionError(
            "Browser tool classification mismatch: missing=%s stale=%s overlaps=%s" % (missing, stale, overlaps)
        )


def sanitize_web_content(
    content: str | None,
    *,
    available_tools: frozenset[str] | None = None,
) -> str:
    """Normalize and bound the size of untrusted web/tool content.

    Behavior (post R5-P0-G):

    1. None/empty guard — accept ``content: str | None`` safely (INT-09).
    2. Unicode NFKC normalization — collapses compatibility homoglyphs
       while preserving CJK/Arabic round-trip (INT-19). This is a charset
       normalization, not a content classifier.
    3. Length truncation — bound prompt cost on large web payloads
       (INT-05). The raw content (truncated) is returned verbatim; the
       model sees it as the tool emitted it.

    What is NO LONGER done here (deleted in R5-P0-G):
      * regex matching of "injection patterns" and ``[REDACTED]`` rewrite
      * stripping of ``[WEB_CONTENT_*]`` / ``UNTRUSTED_WEB_CONTENT`` markers
      * wrapping the content in a prose injection-warning prelude
      * appending a high-risk-tool warning when send/phone tools are active

    Prompt-injection defense for tool-result content lives in the unified
    system prompt (services/llm/prompts/viola_unified.py): the model is
    told to flag suspected injection. That mirrors Claude Code TS
    (src/constants/prompts.ts:191) and respects CLAUDE.md "Viola's runtime
    must trust the model".

    The ``available_tools`` parameter is kept for source-level back-compat
    with older call sites but is no longer used. The structural taint
    gate on tool *names* (``BrowserTaintTracker.check_tool``) remains in
    this module and is the deterministic boundary for high-risk tools
    after browser content enters context.
    """
    # INT-09: None-safe guard — return "" on None/empty rather than raising
    if not content:
        return content or ""

    # NFKC preserves non-Latin scripts (Korean, Japanese, Arabic) round-trip
    # while still collapsing compatibility homoglyphs like fullwidth Latin
    # into their canonical ASCII form.
    content = unicodedata.normalize("NFKC", content)

    # Bound prompt cost on adversarially large web payloads.
    if len(content) > _MAX_WEB_CONTENT_LENGTH:
        content = content[:_MAX_WEB_CONTENT_LENGTH] + "\n[TRUNCATED]"

    return content


def last_action_was_browser(tools_called: list[str]) -> bool:
    """Check if the most recent non-trivial tool call was a browser tool."""
    for tool_name in reversed(tools_called):
        if tool_name in BROWSER_CONTENT_TOOLS:
            return True
        if tool_name != "think":
            return False
    return False


# ---------------------------------------------------------------------------
# Deterministic browser taint tracker (M3 defense)
# ---------------------------------------------------------------------------


class BrowserTaintTracker:
    """Track whether untrusted content has entered the LLM context.

    Despite the legacy name, this tracks taint from EVERY untrusted content
    source — browser/web pages AND email, calendar, memory, and file content
    (SEC-001). When tainted, high-risk tool calls are blocked with a
    deterministic code gate that cannot be bypassed by prompt injection. When
    the taint came from a *non-browser* source, navigation/GET-capable tools
    are gated too, to close exfil-via-navigation (SEC-004).

    This is a structural gate on tool *names* — it never classifies the content
    of tool results, the user's query, or the model's output.
    """

    def __init__(self) -> None:
        self._tainted: bool = False
        self._taint_source: str = ""
        # True once any NON-browser untrusted source (email/calendar/memory/
        # file) has tainted the context. Drives navigation-sink gating without
        # breaking ordinary multi-page browsing under browser-only taint.
        self._non_browser_tainted: bool = False

    @property
    def is_tainted(self) -> bool:
        """Whether untrusted content has entered the LLM context."""
        return self._tainted

    @property
    def taint_source(self) -> str:
        """The tool that caused the taint (for diagnostics)."""
        return self._taint_source

    @property
    def non_browser_tainted(self) -> bool:
        """Whether a non-browser untrusted source tainted the context."""
        return self._non_browser_tainted

    def _classify_source(self, source_tool: str | None) -> str:
        if is_non_browser_untrusted_source(source_tool):
            return "non_browser"
        if is_browser_page_content_source(source_tool):
            return "browser"
        # Unknown sources are treated as browser-equivalent: they still taint
        # high-risk tools but do not gate navigation, matching the legacy
        # browser-only behavior for any name not in the source taxonomy.
        return "browser"

    def mark_tainted(self, source_tool: str, *, category: str | None = None) -> None:
        """Mark the context as tainted by untrusted content.

        ``category`` ("browser" or "non_browser") is inferred from the source
        tool name when not supplied. A non-browser taint additionally enables
        the navigation-sink gate (SEC-004).
        """
        if category is None:
            category = self._classify_source(source_tool)
        if not self._tainted:
            logger.info(
                "Content taint set: source=%s category=%s",
                source_tool,
                category,
            )
        elif category == "non_browser" and not self._non_browser_tainted:
            logger.info(
                "Content taint escalated to non-browser: source=%s",
                source_tool,
            )
        self._tainted = True
        self._taint_source = source_tool
        if category == "non_browser":
            self._non_browser_tainted = True

    def restore_from_history(self, messages: list[dict[str, object]] | None) -> None:
        """Re-derive taint from prior-turn history (SEC-003).

        Taint is recreated fresh per agent-loop run, so injected content that
        survives in the conversation history would otherwise bypass the gate on
        the next turn. This scans the incoming message chain for assistant
        frames that called an untrusted content SOURCE tool and re-marks taint
        accordingly. It keys off the recorded tool *name* (structural
        provenance) — never the content — so the gate persists exactly as long
        as the tainted tool result remains in context (and clears once it is
        compacted out).
        """
        for message in messages or []:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for name in _tool_names_in_assistant_message(message):
                if is_untrusted_content_source(name):
                    self.mark_tainted(name)

    def reset(self) -> None:
        """Clear the taint flag."""
        if self._tainted:
            logger.debug("Content taint cleared")
        self._tainted = False
        self._taint_source = ""
        self._non_browser_tainted = False

    def check_tool(self, tool_name: str | None) -> str | None:
        """Check if a tool is blocked due to taint.

        INT-09 None-safety: ``tool_name`` may be ``None`` (e.g. during early
        tool dispatch when the tool name has not yet been resolved). A
        missing name cannot match the gated sets and must NOT raise — return
        ``None`` so the caller continues without spurious blocking.

        Returns an error message if blocked, or None if safe.
        """
        if not self._tainted or not tool_name:
            return None

        if _is_high_risk_tool_name(tool_name):
            logger.warning(
                "TAINT_GATE: blocking high-risk '%s' after untrusted content from '%s'",
                tool_name,
                self._taint_source,
            )
            return (
                "BLOCKED: '%s' cannot be called while untrusted content "
                "(web, email, calendar, memory, or file) is in context "
                "(prompt injection defense). The user must explicitly request "
                "this action in a new message." % tool_name
            )

        if self._non_browser_tainted and _is_navigation_sink_name(tool_name):
            logger.warning(
                "TAINT_GATE: blocking navigation sink '%s' after non-browser content from '%s'",
                tool_name,
                self._taint_source,
            )
            return (
                "BLOCKED: '%s' cannot fetch an external URL/query while "
                "untrusted non-browser content (email, calendar, memory, or "
                "file) is in context (exfiltration defense). The user must "
                "explicitly request this navigation in a new message." % tool_name
            )

        return None
