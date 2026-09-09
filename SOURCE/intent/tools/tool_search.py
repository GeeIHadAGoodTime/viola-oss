"""Tool search for deferred MCP tool schemas.

Used by the ``ToolSearch`` MCP tool to search deferred name/description
references and materialize selected full schemas on demand. The model receives
the discovery tool plus compact deferred references up front, not the full MCP
schema catalog.

BM25-style scoring:
- Tokenise query and each tool's name + description into lowercase words.
- Score = sum of IDF-weighted term overlap between query and tool corpus.
- IDF approximated as log(N / (df + 0.5)) where df = document frequency.
- Returns top-N matches by tool name; the provider mapper renders them as
  minimal ``tool_reference`` blocks.
"""

from __future__ import annotations

import contextvars
import math
import re
from collections.abc import Mapping
from typing import Any

from core.logging_config import get_logger
from intent.tools.deferred_tool_schemas import (
    RESOLUTION_RESOLVABLE,
    DeferredToolPool,
    ToolSchemaRef,
    tool_reference_resolution_status,
)

logger = get_logger(__name__)

# Module-level hub reference set by ai_controller after hub initialisation,
# following the same pattern as intent/tools/self_management.py.
_mcp_hub: Any = None
_ctx_deferred_tool_pool: contextvars.ContextVar[DeferredToolPool | None] = contextvars.ContextVar(
    "deferred_tool_pool",
    default=None,
)


def set_mcp_hub(hub: Any) -> None:
    """Wire the live MCPClientHub instance into this module.

    Called by ``ai_controller._ensure_mcp_hub`` so ``tool_search_handler``
    can read live tool schemas at call time without importing the hub at
    module-load (which would create a circular import).
    """
    global _mcp_hub
    _mcp_hub = hub


def set_deferred_tool_pool(
    pool: DeferredToolPool | None,
) -> contextvars.Token[DeferredToolPool | None]:
    """Publish the request-scoped deferred tool pool for tool_search."""
    return _ctx_deferred_tool_pool.set(pool)


def reset_deferred_tool_pool(token: contextvars.Token[DeferredToolPool | None]) -> None:
    """Restore the previous request-scoped deferred tool pool."""
    _ctx_deferred_tool_pool.reset(token)


# Number of results to return by default.
_DEFAULT_TOP_N = 7
# Hard ceiling to avoid flooding context.
_MAX_TOP_N = 12

# Simple English stop-words to remove from tokens before scoring.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "do",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "is",
        "it",
        "its",
        "no",
        "not",
        "of",
        "on",
        "or",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
    }
)

# BM25 tuning parameters (standard defaults).
_K1 = 1.5
_B = 0.75


def _tokenise(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric chars, remove stop-words."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return [t for t in raw if t and t not in _STOP_WORDS]


def _build_corpus(
    tool_schemas: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, int], float]:
    """Build token lists, document frequencies, and average doc length.

    Args:
        tool_schemas: Mapping of tool_name -> schema dict with ``name`` and
            ``description`` keys.

    Returns:
        (doc_tokens, df, avg_dl) where:
        - doc_tokens: tool_name -> list of tokens for that tool
        - df: term -> number of documents containing that term
        - avg_dl: average document length in tokens
    """
    doc_tokens: dict[str, list[str]] = {}
    df: dict[str, int] = {}
    total_len = 0

    for tool_name, schema in tool_schemas.items():
        name_part = schema.get("name", tool_name)
        desc_part = schema.get("description", "")

        # Expand parameter names + descriptions for better matching.
        param_parts: list[str] = []
        input_schema = schema.get("inputSchema", {})
        for param_name, param_schema in input_schema.get("properties", {}).items():
            param_parts.append(param_name)
            param_desc = param_schema.get("description", "") if isinstance(param_schema, dict) else ""
            if param_desc:
                param_parts.append(param_desc)

        full_text = " ".join([name_part, desc_part] + param_parts)
        tokens = _tokenise(full_text)
        doc_tokens[tool_name] = tokens
        total_len += len(tokens)

        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    n_docs = len(doc_tokens)
    avg_dl = total_len / max(1, n_docs)
    return doc_tokens, df, avg_dl


def _bm25_scores(
    query_tokens: list[str],
    doc_tokens: dict[str, list[str]],
    df: dict[str, int],
    avg_dl: float,
) -> dict[str, float]:
    """Compute BM25 scores for all documents against the query.

    Args:
        query_tokens: Tokenised query terms.
        doc_tokens: Per-document token lists.
        df: Term document-frequency mapping.
        avg_dl: Average document length.

    Returns:
        Mapping of tool_name -> BM25 score (0.0 for no match).
    """
    n_docs = len(doc_tokens)
    scores: dict[str, float] = {}

    for tool_name, tokens in doc_tokens.items():
        dl = len(tokens)
        # Build term-frequency map for this document.
        tf_map: dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        score = 0.0
        for term in set(query_tokens):
            if term not in tf_map:
                continue
            tf = tf_map[term]
            doc_freq = df.get(term, 0)
            # IDF component (add 0.5 smoothing).
            idf = math.log((n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)
            # BM25 term score.
            numerator = tf * (_K1 + 1)
            denominator = tf + _K1 * (1 - _B + _B * dl / max(1.0, avg_dl))
            score += idf * (numerator / denominator)

        if score > 0.0:
            scores[tool_name] = score

    return scores


def search_tools(
    query: str,
    tool_schemas: dict[str, dict[str, Any]],
    top_n: int = _DEFAULT_TOP_N,
) -> list[dict[str, Any]]:
    """Search MCP tool schemas using BM25-style keyword ranking.

    Args:
        query: Natural-language description of what the agent needs.
        tool_schemas: Live tool schema dict from ``MCPClientHub._tool_schemas``.
            Each value must have at least ``name`` and ``description`` keys.
        top_n: Number of top results to return (clamped to ``_MAX_TOP_N``).

    Returns:
        List of up to ``top_n`` tool schema dicts (name, description,
        inputSchema), sorted by relevance descending.  Internal ``_``-prefixed
        keys are stripped.
    """
    if not query or not query.strip():
        logger.warning("tool_search called with empty query")
        return []

    top_n = max(1, min(top_n, _MAX_TOP_N))

    # Filter out hidden/internal schemas.
    visible = {k: v for k, v in tool_schemas.items() if not k.startswith("_") and "name" in v}

    if not visible:
        return []

    query_tokens = _tokenise(query)
    if not query_tokens:
        return []

    doc_tokens, df, avg_dl = _build_corpus(visible)
    scores = _bm25_scores(query_tokens, doc_tokens, df, avg_dl)

    if not scores:
        # No BM25 overlap — fall back to tools that share any raw query word.
        fallback: list[dict[str, Any]] = []
        lower_q = query.lower()
        for tool_name, schema in visible.items():
            tool_str = (schema.get("name", "") + " " + schema.get("description", "")).lower()
            if any(w in tool_str for w in lower_q.split()):
                fallback.append({k: v for k, v in schema.items() if not k.startswith("_")})
        return fallback[:top_n]

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    results: list[dict[str, Any]] = []
    for tool_name, _score in ranked[:top_n]:
        schema = visible[tool_name]
        results.append({k: v for k, v in schema.items() if not k.startswith("_")})

    logger.debug(
        "tool_search query=%r top_n=%d matched=%d",
        query[:60],
        top_n,
        len(results),
    )
    return results


def _refs_to_search_schemas(refs: list[ToolSchemaRef]) -> dict[str, dict[str, Any]]:
    return {
        ref.name: {
            "name": ref.name,
            "description": ref.description,
            "namespace": ref.namespace,
            "input_schema_hash": ref.input_schema_hash,
            "resolver_id": ref.resolver_id,
        }
        for ref in refs
    }


_MCP_PREFIX = "mcp__"


def _parse_query_grammar(query: str) -> tuple[str, list[str], list[str]]:
    """Parse Claude's ToolSearch query grammar.

    Returns ``(form, items, free_terms)`` where ``form`` is one of:

    - ``"select"`` — ``select:Read,Edit,Grep`` — items are exact deferred-ref
      names to materialize, in order.
    - ``"exact"`` — a single bare token that matches a deferred ref or
      loaded tool by exact name. items = [that name].
    - ``"mcp_prefix"`` — a single token starting with ``mcp__`` (with no
      space/comma); items = [the prefix].
    - ``"required"`` — query contains ``+required`` terms; items = required
      bare terms (lowercased), free_terms = remaining tokens for BM25.
    - ``"keyword"`` — fallthrough BM25 keyword search; free_terms = tokens
      (full original query); items = [].

    Claude TS reference: ``tools/ToolSearchTool/prompt.ts:48-51`` and
    ``tools/ToolSearchTool/ToolSearchTool.ts:194-257``.
    """
    stripped = query.strip()
    if not stripped:
        return ("keyword", [], [])

    lower_stripped = stripped.lower()
    if lower_stripped.startswith("select:"):
        raw_names = stripped.split(":", 1)[1]
        items = [name.strip() for name in re.split(r"[,;\s]+", raw_names) if name.strip()]
        return ("select", items, [])

    tokens = stripped.split()
    if len(tokens) == 1:
        only = tokens[0]
        # mcp__ prefix selector — Claude returns every tool whose name starts
        # with that exact prefix, no scoring.
        if only.startswith(_MCP_PREFIX):
            return ("mcp_prefix", [only], [])
        # Bare exact-name shortcut: any single word that *could* be a tool
        # name (no internal punctuation other than underscore/double-underscore).
        # If the resolver can't match it by name, the handler falls back to
        # BM25 against the full original query.
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*", only):
            return ("exact", [only], [tokens[0]])

    # +required filter: any token starting with '+' is a hard filter.
    if any(tok.startswith("+") and len(tok) > 1 for tok in tokens):
        required: list[str] = []
        free: list[str] = []
        for tok in tokens:
            if tok.startswith("+") and len(tok) > 1:
                required.append(tok[1:].lower())
            else:
                free.append(tok)
        return ("required", required, free)

    return ("keyword", [], tokens)


def _select_refs(query: str, refs: list[ToolSchemaRef], top_n: int) -> list[ToolSchemaRef]:
    """Return refs selected by Claude's full ToolSearch query grammar.

    F-044 (R3-C): ports ``tools/ToolSearchTool/ToolSearchTool.ts:194-257``:
    ``select:``, bare exact-name, ``mcp__`` prefix, ``+required`` filter,
    and BM25 keyword search as the fallthrough.
    """
    by_name = {ref.name: ref for ref in refs}
    form, items, free_terms = _parse_query_grammar(query)

    if form == "select":
        selected: list[ToolSchemaRef] = []
        for name in items:
            ref = by_name.get(name)
            if ref is not None and ref not in selected:
                selected.append(ref)
            if len(selected) >= top_n:
                break
        return selected

    if form == "exact":
        # Direct exact-name match against deferred refs.
        name = items[0]
        ref = by_name.get(name)
        if ref is not None:
            return [ref]
        # Fall through to keyword search using the bare token as the query.
        return _select_refs_bm25(free_terms, refs, by_name, top_n)

    if form == "mcp_prefix":
        prefix = items[0]
        matched = [ref for ref in refs if ref.name.startswith(prefix)]
        # Stable order: by name.
        matched.sort(key=lambda ref: ref.name)
        return matched[:top_n]

    if form == "required":
        required = items
        filtered: list[ToolSchemaRef] = []
        for ref in refs:
            corpus = (ref.name + " " + ref.description).lower()
            if all(term in corpus for term in required):
                filtered.append(ref)
        if not free_terms:
            # All free signal was in the +required terms — return filtered.
            filtered.sort(key=lambda ref: ref.name)
            return filtered[:top_n]
        return _select_refs_bm25(free_terms, filtered, {ref.name: ref for ref in filtered}, top_n)

    # Keyword fallthrough.
    return _select_refs_bm25(free_terms or query.split(), refs, by_name, top_n)


def _tool_name_from_visible(tool: Mapping[str, Any]) -> str:
    name = tool.get("name")
    if isinstance(name, str):
        return name.strip()
    function = tool.get("function")
    if isinstance(function, Mapping):
        fn_name = function.get("name")
        if isinstance(fn_name, str):
            return fn_name.strip()
    return ""


def _visible_tool_name_lookup(visible_tools: list[dict[str, Any]]) -> dict[str, str]:
    """Return exact visible-name/alias lookups mapped to canonical names."""
    names: dict[str, str] = {}
    for tool in visible_tools:
        if not isinstance(tool, Mapping):
            continue
        canonical = _tool_name_from_visible(tool)
        if not canonical:
            continue
        names.setdefault(canonical, canonical)
        aliases = tool.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    names.setdefault(alias.strip(), canonical)
    return names


def _known_tool_name(
    requested: str,
    deferred_by_name: dict[str, ToolSchemaRef],
    visible_by_name: dict[str, str],
    *,
    case_insensitive: bool = False,
) -> str | None:
    ref = deferred_by_name.get(requested)
    if ref is not None:
        return ref.name
    visible = visible_by_name.get(requested)
    if visible is not None:
        return visible
    if not case_insensitive:
        return None
    requested_lower = requested.lower()
    for name, ref in deferred_by_name.items():
        if name.lower() == requested_lower:
            return ref.name
    for name, canonical in visible_by_name.items():
        if name.lower() == requested_lower:
            return canonical
    return None


def _select_tool_names(
    query: str,
    refs: list[ToolSchemaRef],
    visible_by_name: dict[str, str],
    top_n: int,
) -> list[str]:
    """Return ToolSearch match names, including no-op visible selections.

    Claude accepts selecting an already-loaded tool as a harmless no-op. Keep
    all fuzzy search paths scoped to deferred refs; only exact ``select:`` and
    bare exact-name forms may return a visible tool name.
    """
    by_name = {ref.name: ref for ref in refs}
    form, items, free_terms = _parse_query_grammar(query)

    if form == "select":
        selected: list[str] = []
        for name in items:
            match = _known_tool_name(name, by_name, visible_by_name)
            if match is not None and match not in selected:
                selected.append(match)
            if len(selected) >= top_n:
                break
        return selected

    if form == "exact":
        name = items[0]
        match = _known_tool_name(name, by_name, visible_by_name, case_insensitive=True)
        if match is not None:
            return [match]
        return [ref.name for ref in _select_refs_bm25(free_terms, refs, by_name, top_n)]

    return [ref.name for ref in _select_refs(query, refs, top_n=top_n)]


def _select_refs_bm25(
    free_terms: list[str],
    refs: list[ToolSchemaRef],
    by_name: dict[str, ToolSchemaRef],
    top_n: int,
) -> list[ToolSchemaRef]:
    """BM25 fallthrough over a (possibly pre-filtered) ref list."""
    if not refs:
        return []
    search_index = _refs_to_search_schemas(refs)
    bm25_query = " ".join(free_terms) if free_terms else ""
    if not bm25_query.strip():
        return []
    matches = search_tools(bm25_query, search_index, top_n=top_n)
    return [by_name[match["name"]] for match in matches if match.get("name") in by_name]


def _get_deferred_pool() -> DeferredToolPool:
    """Return the request-scoped deferred pool published by the agent loop.

    F-013 (R3-C / Claude `tools/ToolSearchTool/ToolSearchTool.ts:328-333`):
    deferred tools are derived from the explicit `ToolUseContext.options.tools`
    array; no global/ambient `core.user_context` lookup. The agent loop owns
    publishing the pool via `set_deferred_tool_pool()` before each turn — if
    that wiring is missing, this handler must fail loudly rather than silently
    fall back to ambient user state (which is exactly the cross-tenant
    confusion class S7-09 flagged).
    """
    pool = _ctx_deferred_tool_pool.get()
    if pool is not None:
        return pool
    raise RuntimeError("request-scoped deferred tool pool is unavailable")


async def tool_search_handler(
    query: str,
    top_n: int = _DEFAULT_TOP_N,
) -> Any:
    """MCP handler for the ToolSearch tool.

    Retrieves the request-scoped deferred pool and runs BM25 scoring over the
    compact references. Returns Claude-shaped match names so subsequent
    provider turns can load only what was selected.

    Args:
        query: What capability the agent is looking for.
        top_n: Max results to return (default 7, max 12).
    """
    from intent.tool_types import ToolResult

    if not query or not query.strip():
        return ToolResult(ok=False, error="query must be a non-empty string")

    top_n = max(1, min(int(top_n), _MAX_TOP_N))

    if _mcp_hub is None:
        return ToolResult(
            ok=False,
            error="MCP hub not initialised - tool_search is unavailable",
        )

    try:
        pool = _get_deferred_pool()
    except Exception as exc:
        logger.exception("tool_search: failed to retrieve deferred tool pool")
        return ToolResult(ok=False, error="Could not retrieve deferred tool pool: %s" % exc)

    refs = list(pool.deferred_refs)
    pool_user_id = str(pool.user_id or "").strip()
    if refs and not pool_user_id:
        return ToolResult(ok=False, error="Deferred tool pool is missing user scope")
    # F-013 (R3-C): no ambient `core.user_context` cross-check. The pool's
    # `user_id` is the request-scoped tenant identity established by the
    # caller (agent loop) when it published the pool; an additional global
    # lookup would let unrelated ambient state break a valid request, and
    # if the ambient value is absent the check is skipped anyway — so it is
    # not a reliable guard. Claude's TS `ToolSearchTool.ts:328-333` reads
    # only the explicit `ToolUseContext.options.tools` for the same reason.

    visible_by_name = _visible_tool_name_lookup(list(pool.visible_tools))
    results = _select_tool_names(query, refs, visible_by_name, top_n=top_n)
    # issue #2094: selecting an already-loaded tool stays a harmless no-op (see
    # ``_select_tool_names``), but reporting it as a plain match made the result
    # indistinguishable from a real deferred materialization — the model got no
    # evidence the search had been unnecessary, so the same wasted turn repeated
    # every session. Name the no-op part of the answer instead of hiding it.
    deferred_names = {ref.name for ref in refs}
    already_loaded = [name for name in results if name not in deferred_names]

    # A match is the model's evidence that the tool is callable next turn: each
    # name becomes a ``tool_reference`` block (intent/tool_result_mappers.py)
    # that the following turn expands back into a schema. That expansion can
    # find nothing -- the registry is an LRU and the entry may be gone, or the
    # schema may have changed under the reference -- and it used to skip such a
    # reference silently. The search then reported a match for a schema that
    # never arrived, and the model called a tool it had been told was ready.
    # Check the same resolution the next turn will run, and report a match only
    # where it succeeds.
    deferred_matches = [name for name in results if name in deferred_names]
    resolution = tool_reference_resolution_status(deferred_matches, user_id=pool_user_id)
    unresolvable = [
        {"name": name, "reason": reason} for name, reason in resolution.items() if reason != RESOLUTION_RESOLVABLE
    ]
    unresolvable_names = {item["name"] for item in unresolvable}
    loadable = [name for name in results if name not in unresolvable_names]

    if unresolvable:
        logger.warning(
            "tool_search: %d of %d matched deferred tools cannot be materialised (%s)",
            len(unresolvable),
            len(deferred_matches),
            ", ".join("%s=%s" % (item["name"], item["reason"]) for item in unresolvable),
        )

    data: dict[str, Any] = {
        "query": query,
        "matches": loadable,
        "already_loaded": already_loaded,
        "total_deferred_tools": len(refs),
    }
    if unresolvable:
        data["unresolvable"] = unresolvable

    if not loadable and unresolvable:
        # Every match this search found is a dead reference, so there is
        # nothing for the model to call and nothing to wait for.
        return ToolResult(
            ok=False,
            error=(
                "Matched %d deferred tool(s), but none of their schemas can be loaded: %s."
                % (
                    len(unresolvable),
                    ", ".join("%s (%s)" % (item["name"], item["reason"]) for item in unresolvable),
                )
            ),
            error_category="deferred_schema_unavailable",
            data=data,
        )

    return ToolResult(ok=True, data=data)
