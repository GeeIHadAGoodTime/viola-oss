"""Web search tool for the agent.

``web_search`` returns raw discovery snippets with stable result ids. ``web_read``
can read selected public result URLs; browser tools handle JavaScript-heavy,
login, commerce, or interactive pages.

Where the results come from:

* The cloud runs SearXNG next to the API, on a private Docker network, and uses it
  whenever ``VIOLA_SEARXNG_URL`` is set.
* A desktop has no SearXNG and never will: it is a Linux server program with its
  own engine pool, not something an installer can put on a Windows tablet, and
  sending every desktop's searches through the cloud's one would add a server hop,
  put users' queries on our servers, and give every engine one shared address to
  rate-limit. So a desktop asks the search engines itself, from the user's own
  address, which is a first-class path and not a degraded one: DuckDuckGo Lite with
  the full query (``site:``, time range, next pages), then Bing's RSS feed if
  DuckDuckGo shows a bot check, errors or finds nothing. News asks Google News' RSS
  feed first. A bot check is never answered: that engine is left alone for a while
  and the next one is asked.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx

from config.settings import settings
from core.constants import TIMEOUT_10_MINUTES
from core.logging_config import get_logger
from intent.tool_types import ToolResult
from services.api_cache import get_api_cache, public_cache_key

logger = get_logger(__name__)

_SEARCH_TIMEOUT = 10.0
_WEB_SEARCH_CACHE_TTL_SECONDS = int(TIMEOUT_10_MINUTES)
_DOMAIN_AGE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_DDG_API_URL = "https://api.duckduckgo.com/"
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_BING_SEARCH_URL = "https://www.bing.com/search"
_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
_DEFAULT_CATEGORY = "general"
_VALID_TIME_RANGES: frozenset[str] = frozenset({"day", "week", "month", "year"})
_DDG_TIME_FILTERS: dict[str, str] = {"day": "d", "week": "w", "month": "m", "year": "y"}
_GOOGLE_NEWS_WHEN: dict[str, str] = {"day": "1d", "week": "7d", "month": "30d", "year": "1y"}
_MAX_DDG_PAGE = 5
_NEWS_DEFAULT_TIME_RANGE = "week"
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xml",
    "Accept-Language": "en-US,en;q=0.9",
}
# An engine that showed a bot check or a rate limit is left alone this long.
_ENGINE_COOLDOWN_SECONDS = 15 * 60
# Domain age is a nice-to-have signal: it must not hold up the answer.
_DOMAIN_AGE_BUDGET_SECONDS = 1.5
_TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "_hsenc",
        "_hsmi",
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "ref",
        "ref_src",
        "spm",
        "vero_conv",
        "vero_id",
        "yclid",
    }
)
_COMMON_SECOND_LEVEL_SUFFIXES: frozenset[str] = frozenset(
    {"ac", "co", "com", "edu", "gob", "gov", "gouv", "net", "org"}
)
_GOVERNMENT_LABELS: frozenset[str] = frozenset({"gov", "gob", "gouv", "go", "mil"})
_EDUCATION_LABELS: frozenset[str] = frozenset({"edu", "ac"})
_OFFICIAL_DOC_LABELS: frozenset[str] = frozenset(
    {
        "api",
        "apis",
        "developer",
        "developers",
        "docs",
        "documentation",
        "manual",
        "reference",
    }
)
_UGC_LABELS: frozenset[str] = frozenset({"answers", "community", "discuss", "forum", "forums", "questions"})
_COMMERCIAL_TLDS: frozenset[str] = frozenset(
    {
        "ai",
        "app",
        "biz",
        "co",
        "com",
        "io",
        "net",
        "shop",
        "store",
        "tech",
        "xyz",
    }
)
_SPECIFIC_DATA_RE = re.compile(
    r"(?ix)"
    r"(\$|\u20ac|\u00a3|\u00a5)\s?\d|"
    r"\b\d+(?:[.,]\d+)?\s?(?:%|percent|usd|eur|gbp|mph|mi|km|lbs?|kg|hours?|mins?|minutes?)\b|"
    r"\b(?:19|20)\d{2}\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b[1-5]\d{2}\s+(?:status|error|response|code)\b"
)
_CATEGORY_ALIASES: dict[str, str] = {
    "": _DEFAULT_CATEGORY,
    "all": _DEFAULT_CATEGORY,
    "general": _DEFAULT_CATEGORY,
    "web": _DEFAULT_CATEGORY,
    "internet": _DEFAULT_CATEGORY,
    "local": "maps",
    "location": "maps",
    "locations": "maps",
    "map": "maps",
    "maps": "maps",
    "place": "maps",
    "places": "maps",
    "restaurants": "maps",
    "restaurant": "maps",
    "shopping": "shopping",
    "shop": "shopping",
    "news": "news",
    "images": "images",
    "image": "images",
    "videos": "videos",
    "video": "videos",
}


class _EngineBlocked(Exception):
    """A search engine showed a bot check or a rate limit. It is never answered."""


# engine name -> time.monotonic() until which it is not asked.
_engine_cooldowns: dict[str, float] = {}


def _engine_cooling_down(engine: str) -> bool:
    return _engine_cooldowns.get(engine, 0.0) > time.monotonic()


def _reset_engine_cooldowns() -> None:
    _engine_cooldowns.clear()


def clear_web_search_counter() -> None:
    """Compatibility no-op retained for existing task-start hooks."""


def notify_browser_navigate() -> None:
    """Compatibility no-op retained for existing browser hooks."""


def _normalize_page(page: int | str | None) -> int:
    try:
        value = int(page or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _normalize_time_range(time_range: str | None) -> str | None:
    normalized = (time_range or "").strip().lower()
    if not normalized:
        return None
    return normalized if normalized in _VALID_TIME_RANGES else None


def _normalize_category(category: str | None) -> str:
    raw = (category or _DEFAULT_CATEGORY).strip().lower().replace("_", " ")
    return _CATEGORY_ALIASES.get(raw, raw or _DEFAULT_CATEGORY)


def _searxng_base_url() -> str:
    return str(getattr(settings, "searxng_url", "") or "").strip().rstrip("/")


def _clean_text(value: Any, *, limit: int | None = None) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _strip_tracking_params(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_PARAM_NAMES:
            continue
        query_items.append((key, value))
    clean_query = urlencode(sorted(query_items), doseq=True)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            clean_query,
            "",
        )
    )


def _normalized_host(hostname: str | None) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _canonical_result_url(url: str) -> str:
    clean_url = _strip_tracking_params(url)
    parsed = urlparse(clean_url)
    if not parsed.scheme or not parsed.netloc:
        return clean_url
    host = _normalized_host(parsed.hostname)
    if not host:
        return clean_url
    netloc = host
    if parsed.port and not (
        (parsed.scheme.lower() == "https" and parsed.port == 443)
        or (parsed.scheme.lower() == "http" and parsed.port == 80)
    ):
        netloc = "%s:%d" % (host, parsed.port)
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, parsed.params, parsed.query, ""))


def _canonical_content_key(url: str) -> str:
    canonical = _canonical_result_url(url)
    parsed = urlparse(canonical)
    if not parsed.netloc:
        return canonical
    return urlunparse(
        (
            "",
            _normalized_host(parsed.hostname),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _registered_domain(hostname: str | None) -> str | None:
    host = _normalized_host(hostname)
    if not host or re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", host):
        return None
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and labels[-2] in _COMMON_SECOND_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _host_labels(hostname: str | None) -> set[str]:
    return {label for label in _normalized_host(hostname).split(".") if label}


def _tld_class(url: str, title: str = "") -> str:
    parsed = urlparse(url)
    host = _normalized_host(parsed.hostname)
    if not host:
        return "unknown"
    labels = _host_labels(host)
    tld = host.rsplit(".", 1)[-1]
    path_labels = {part for part in re.split(r"[^a-z0-9]+", parsed.path.lower()) if part}
    title_labels = {part for part in re.split(r"[^a-z0-9]+", title.lower()) if part}

    if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
        return "wikipedia"
    if labels & _GOVERNMENT_LABELS:
        return "government"
    if labels & _EDUCATION_LABELS:
        return "education"
    if (labels | path_labels) & _OFFICIAL_DOC_LABELS:
        return "official_docs"
    if (labels | path_labels) & _UGC_LABELS:
        return "ugc"
    if tld in _COMMERCIAL_TLDS:
        return "commercial"
    return "unknown"


def _snippet_has_specific_data(title: str, snippet: str) -> bool:
    return bool(_SPECIFIC_DATA_RE.search("%s %s" % (title, snippet)))


def _engine_names(result: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    raw_engines = result.get("engines")
    if isinstance(raw_engines, list):
        for engine in raw_engines:
            text = _clean_text(engine, limit=80).lower()
            if text:
                names.add(text)
    raw_engine = result.get("engine")
    if raw_engine not in (None, "", []):
        text = _clean_text(raw_engine, limit=80).lower()
        if text:
            names.add(text)
    source = _clean_text(result.get("source"), limit=80).lower()
    if source and not names:
        names.add(source)
    return names


def _merge_result(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    if len(str(incoming.get("title") or "")) > len(str(existing.get("title") or "")):
        existing["title"] = incoming.get("title") or existing.get("title")
    if len(str(incoming.get("snippet") or "")) > len(str(existing.get("snippet") or "")):
        existing["snippet"] = incoming.get("snippet") or existing.get("snippet")

    merged_engines = sorted(_engine_names(existing) | _engine_names(incoming))
    if merged_engines:
        existing["engines"] = merged_engines
    for key in ("category", "publishedDate", "parsed_url", "thumbnail", "template"):
        if existing.get(key) in (None, "", []) and incoming.get(key) not in (
            None,
            "",
            [],
        ):
            existing[key] = incoming[key]


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rank, item in enumerate(results, start=1):
        url = _clean_text(item.get("url"))
        canonical_url = _canonical_result_url(url)
        key = _canonical_content_key(canonical_url)
        if not key:
            key = "rank:%d:%s" % (rank, _clean_text(item.get("title"), limit=80))
        normalized = dict(item)
        normalized["url"] = canonical_url or url
        normalized["canonical_url"] = canonical_url or url
        normalized["original_rank"] = min(rank, int(normalized.get("original_rank") or rank))
        normalized.setdefault("redirect_chain", [])
        if key in deduped:
            _merge_result(deduped[key], normalized)
            deduped[key]["deduped_count"] = int(deduped[key].get("deduped_count") or 1) + 1
            deduped[key]["original_rank"] = min(
                int(deduped[key].get("original_rank") or rank),
                int(normalized.get("original_rank") or rank),
            )
        else:
            normalized["deduped_count"] = 1
            deduped[key] = normalized
            order.append(key)
    return [deduped[key] for key in order]


def _domain_age_cache_key(domain: str) -> str:
    return public_cache_key("web_search_domain_age:v1", {"domain": domain})


def _cached_domain_age_days(domain: str) -> int | None:
    cached = get_api_cache().get_public(_domain_age_cache_key(domain))
    if not isinstance(cached, dict):
        return None
    value = cached.get("domain_age_days")
    return value if isinstance(value, int) else None


def _cache_domain_age_days(domain: str, age_days: int) -> None:
    get_api_cache().set_public(
        _domain_age_cache_key(domain),
        {"domain_age_days": age_days},
        ttl_seconds=_DOMAIN_AGE_CACHE_TTL_SECONDS,
    )


def _rdap_event_date(payload: dict[str, Any]) -> datetime | None:
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        action = str(event.get("eventAction") or "").lower()
        if "registration" not in action and action not in {
            "registered",
            "create",
            "created",
        }:
            continue
        raw_date = str(event.get("eventDate") or "").strip()
        if not raw_date:
            continue
        try:
            return datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


async def _fetch_domain_age_days(client: httpx.AsyncClient, domain: str) -> int | None:
    cached = _cached_domain_age_days(domain)
    if cached is not None:
        return cached
    try:
        # follow_redirects=True is load-bearing: rdap.org is the IANA-maintained
        # redirector and replies with 302 to TLD-specific RDAP servers
        # (rdap.verisign.com for .com, rdap.nic.gov for .gov, etc). Without
        # follow_redirects the 302's empty body fails .json() with
        # JSONDecodeError, the bare except swallows it, every domain_age_days
        # comes back None, and the trust score collapses to TLD-class-only
        # differentiation. Timeout bumped from 3s to 8s because rdap.nic.gov in
        # particular is slow on cold cache.
        response = await client.get(
            "https://rdap.org/domain/%s" % domain,
            timeout=8.0,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return None
        created_at = _rdap_event_date(response.json())
    except (httpx.HTTPError, ValueError):
        # G201 + #5: surface the cause so we don't silently degrade trust
        # scoring. RDAP outages, redirect failures, timeouts, and JSON decode
        # errors all funnel through here.
        logger.exception("Domain age lookup failed for %s", domain)
        return None
    if created_at is None:
        return None
    age_days = max(0, (datetime.now(UTC) - created_at.astimezone(UTC)).days)
    _cache_domain_age_days(domain, age_days)
    return age_days


# Removed _score_result (2026-05-26): the aggregate "trust_score" it produced
# was a hand-tuned weighted classifier (gov=34, edu=26, ... ugc=2) that
# pre-cooked a verdict, silently collapsed to noise when any input component
# degraded (rdap.org / SearXNG engine suspensions), and boxed the model's
# judgment — see "Don't box the lens" in CLAUDE.md. Per-result we now emit
# the raw fields (tld_class, domain_age_days, engine_consensus_count,
# snippet_has_specific_data, https, and redirect_chain) and let the model
# judge.


async def _post_process_results(
    client: httpx.AsyncClient,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped = _dedupe_results(results)

    domain_to_items: dict[str, list[dict[str, Any]]] = {}
    for item in deduped:
        parsed = urlparse(str(item.get("canonical_url") or item.get("url") or ""))
        domain = _registered_domain(parsed.hostname)
        item["domain_age_days"] = None
        if domain:
            domain_to_items.setdefault(domain, []).append(item)

    domains = list(domain_to_items)[:12]
    if domains:
        lookups = {asyncio.ensure_future(_fetch_domain_age_days(client, domain)): domain for domain in domains}
        done, pending = await asyncio.wait(lookups, timeout=_DOMAIN_AGE_BUDGET_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            age = None if task.cancelled() or task.exception() is not None else task.result()
            if isinstance(age, int):
                for item in domain_to_items[lookups[task]]:
                    item["domain_age_days"] = age

    for item in deduped:
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        item["tld_class"] = _tld_class(str(item.get("canonical_url") or item.get("url") or ""), title)
        engines = sorted(_engine_names(item))
        if engines:
            item["engines"] = engines
        item["engine_consensus_count"] = max(1, len(engines))
        item["snippet_has_specific_data"] = _snippet_has_specific_data(title, snippet)
        item.setdefault("redirect_chain", [])

    # Sort by SearXNG's relevance ranking (original_rank). The raw signals
    # tld_class / domain_age_days / engine_consensus_count / snippet_has_specific_data
    # / https / redirect_chain are emitted per-result so the model picks based
    # on the full evidence. The aggregate trust_score was
    # removed (2026-05-26) because it pre-cooked a verdict with hand-tuned
    # weights, silently collapsed when any component degraded, and boxed the
    # model's judgment — "enrich the data, don't replace the decision."
    deduped.sort(key=lambda item: int(item.get("original_rank") or 9999))
    return deduped


def _result_from_searxng_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = _clean_text(item.get("url"))
    title = _clean_text(item.get("title")) or url
    snippet = _clean_text(item.get("content") or item.get("snippet"), limit=600)
    if not title and not snippet and not url:
        return None

    result: dict[str, Any] = {
        "title": title,
        "snippet": snippet,
        "url": url,
        "source": "searxng",
    }
    for key in (
        "engine",
        "engines",
        "category",
        "publishedDate",
        "parsed_url",
        "thumbnail",
        "template",
    ):
        if item.get(key) not in (None, "", []):
            result[key] = item[key]
    return result


def _results_from_searxng_html(html: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    article_pattern = re.compile(
        r"<article\b[^>]*class=['\"][^'\"]*result[^'\"]*['\"][^>]*>(.*?)</article>",
        re.I | re.S,
    )
    for article_match in article_pattern.finditer(html):
        article = article_match.group(1)
        url_match = re.search(
            r"<a\b[^>]*class=['\"]url_header['\"][^>]*href=['\"]([^'\"]+)['\"]",
            article,
            re.I,
        )
        title_match = re.search(
            r"<h3[^>]*>\s*<a\b[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>\s*</h3>",
            article,
            re.I | re.S,
        )
        if title_match and not url_match:
            url = title_match.group(1)
        elif url_match:
            url = url_match.group(1)
        else:
            url = ""

        title = _clean_text(title_match.group(2), limit=180) if title_match else _clean_text(url, limit=180)
        content_match = re.search(r"<p\b[^>]*class=['\"]content['\"][^>]*>(.*?)</p>", article, re.I | re.S)
        snippet = _clean_text(content_match.group(1), limit=600) if content_match else ""

        engine_block = re.search(r"<div\b[^>]*class=['\"]engines['\"][^>]*>(.*?)</div>", article, re.I | re.S)
        engines = []
        if engine_block:
            engines = [
                _clean_text(match, limit=80)
                for match in re.findall(r"<span[^>]*>(.*?)</span>", engine_block.group(1), re.I | re.S)
            ]
            engines = [engine for engine in engines if engine]

        if not title and not snippet and not url:
            continue
        result: dict[str, Any] = {
            "title": title or url,
            "snippet": snippet,
            "url": _clean_text(url),
            "source": "searxng",
        }
        if engines:
            result["engines"] = engines
        results.append(result)
    return results


async def _search_searxng_html(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    category: str,
    *,
    page: int,
    time_range: str | None,
    json_error: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "q": query.strip(),
        "categories": category,
        "pageno": page,
    }
    if time_range:
        params["time_range"] = time_range

    response = await client.get(
        "%s/search" % base_url,
        params=params,
        headers={"Accept": "text/html"},
    )
    response.raise_for_status()
    results = _results_from_searxng_html(response.text)
    metadata = {
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": [],
        "number_of_results": None,
        "page": page,
        "category": category,
        "time_range": time_range,
        "format": "html",
        "json_unavailable": True,
    }
    if json_error:
        metadata["json_error"] = json_error
    return {"results": results, "metadata": metadata}


def _with_result_refs(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item, id="r%d" % index) for index, item in enumerate(results, start=1)]


def _web_search_cache_key(
    *,
    query: str,
    category: str,
    page: int,
    time_range: str | None,
    searxng_url: str,
) -> str:
    return public_cache_key(
        "web_search:v5",
        {
            "query": query,
            "category": category,
            "page": page,
            "time_range": time_range,
            "searxng_url": searxng_url,
        },
    )


def _cached_web_search_result(cache_key: str, query: str) -> ToolResult | None:
    cached = get_api_cache().get_public(cache_key)
    if not isinstance(cached, dict):
        return None
    cached_data = dict(cached)
    cached_data["query"] = query
    cached_data["cached"] = True

    data: dict[str, Any] = {"query": query}
    for key, value in cached_data.items():
        if key != "query":
            data[key] = value
    return ToolResult(ok=True, data=data)


def _cache_web_search_result(cache_key: str, data: dict[str, Any]) -> None:
    # #2776: a degraded bundle (a configured SearXNG is down, so a direct
    # engine stood in) must never be cached at the normal success TTL. Caching
    # it would return the same degraded/possibly-empty result as "success" for
    # the full TTL window even after the primary backend recovers. A desktop
    # with no SearXNG is not degraded, so its results are cached normally.
    if data.get("degraded") or (data.get("backend") != "searxng" and not data.get("results")):
        logger.debug("Not caching degraded or empty web_search result for key=%s", cache_key)
        return
    payload = dict(data)
    payload.pop("query", None)
    payload["cached"] = False
    get_api_cache().set_public(
        cache_key,
        payload,
        ttl_seconds=_WEB_SEARCH_CACHE_TTL_SECONDS,
    )


async def _search_searxng(
    client: httpx.AsyncClient,
    query: str,
    category: str = _DEFAULT_CATEGORY,
    *,
    page: int | str | None = 1,
    time_range: str | None = None,
) -> dict[str, Any]:
    """Query SearXNG and return raw parsed results plus SERP metadata."""

    base_url = _searxng_base_url()
    if not base_url:
        raise RuntimeError("SearXNG is not configured")

    normalized_page = _normalize_page(page)
    normalized_category = _normalize_category(category)
    normalized_time = _normalize_time_range(time_range)
    params: dict[str, Any] = {
        "q": query.strip(),
        "format": "json",
        "categories": normalized_category,
        "pageno": normalized_page,
    }
    if normalized_time:
        params["time_range"] = normalized_time

    try:
        response = await client.get(
            "%s/search" % base_url,
            params=params,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return await _search_searxng_html(
                client,
                base_url,
                query,
                normalized_category,
                page=normalized_page,
                time_range=normalized_time,
                json_error="HTTP 403 for format=json",
            )
        raise
    except ValueError:
        return await _search_searxng_html(
            client,
            base_url,
            query,
            normalized_category,
            page=normalized_page,
            time_range=normalized_time,
            json_error="non-JSON response for format=json",
        )

    raw_items = data.get("results")
    if not isinstance(raw_items, list):
        raw_items = []

    results: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        result = _result_from_searxng_item(item)
        if result is not None:
            results.append(result)

    metadata = {
        "answers": data.get("answers") or [],
        "corrections": data.get("corrections") or [],
        "infoboxes": data.get("infoboxes") or [],
        "suggestions": data.get("suggestions") or [],
        "unresponsive_engines": data.get("unresponsive_engines") or [],
        "number_of_results": data.get("number_of_results"),
        "page": normalized_page,
        "category": normalized_category,
        "time_range": normalized_time,
        "format": "json",
        "json_unavailable": False,
    }
    return {"results": results, "metadata": metadata}


async def _search_instant_answer(
    client: httpx.AsyncClient,
    query: str,
) -> list[dict[str, Any]]:
    """DuckDuckGo Instant Answer: a short abstract, when the query has one."""

    response = await client.get(
        _DDG_API_URL,
        params={
            "q": query.strip(),
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        },
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        logger.warning(
            "DuckDuckGo instant answer returned non-JSON; falling back to lite HTML: %s",
            type(exc).__name__,
        )
        return []

    results: list[dict[str, Any]] = []
    abstract = _clean_text(data.get("AbstractText"), limit=600)
    if abstract:
        results.append(
            {
                "title": _clean_text(data.get("Heading")) or "Result",
                "snippet": abstract,
                "url": _clean_text(data.get("AbstractURL")),
                "source": _clean_text(data.get("AbstractSource")) or "duckduckgo",
            }
        )
    return results


_DDG_LINK_RE = re.compile(r"""<a\b[^>]*class=['"]result-link['"][^>]*>""", re.IGNORECASE)
_DDG_HREF_RE = re.compile(r"""href=['"]([^'"]+)['"]""", re.IGNORECASE)
_DDG_SNIPPET_RE = re.compile(r"""<td[^>]+class=['"]result-snippet['"][^>]*>(.*?)</td>""", re.IGNORECASE | re.DOTALL)
_DDG_FORM_RE = re.compile(r"<form\b.*?</form>", re.IGNORECASE | re.DOTALL)
_HTML_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_HTML_ATTR_RE = re.compile(r"""\b(name|value)=['"]([^'"]*)['"]""", re.IGNORECASE)


def _ddg_target_url(raw_href: str) -> str:
    """The page a DuckDuckGo result points at, or "" for an ad or DuckDuckGo's own page."""
    href = unescape(raw_href).strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    host = _normalized_host(parsed.hostname)
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        if parsed.path.rstrip("/") == "/l":
            return parse_qs(parsed.query).get("uddg", [""])[0]
        return ""
    return href


def _results_from_ddg_lite(html: str) -> list[dict[str, Any]]:
    """Organic results from a Lite page, each with the snippet in its own rows."""
    matches = list(_DDG_LINK_RE.finditer(html))
    results: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end_of_title = html.find("</a>", match.end())
        if end_of_title < 0:
            continue
        href = _DDG_HREF_RE.search(match.group(0))
        url = _ddg_target_url(href.group(1)) if href else ""
        if not url:
            continue
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        snippet = _DDG_SNIPPET_RE.search(html, end_of_title, block_end)
        results.append(
            {
                "title": _clean_text(html[match.end() : end_of_title]),
                "snippet": _clean_text(snippet.group(1), limit=600) if snippet else "",
                "url": _clean_text(url),
                "source": "duckduckgo",
            }
        )
    return results


def _ddg_next_page_fields(html: str) -> dict[str, str] | None:
    """The hidden fields of the page's own "Next Page" form."""
    for form in _DDG_FORM_RE.findall(html):
        if "next page" not in form.lower():
            continue
        fields: dict[str, str] = {}
        for tag in _HTML_INPUT_RE.findall(form):
            attributes = {key.lower(): value for key, value in _HTML_ATTR_RE.findall(tag)}
            if attributes.get("name"):
                fields[attributes["name"]] = unescape(attributes.get("value", ""))
        return fields or None
    return None


async def _ddg_lite_page(client: httpx.AsyncClient, fields: dict[str, str]) -> str:
    response = await client.post(_DDG_LITE_URL, data=fields, headers=_BROWSER_HEADERS)
    # 202 is DuckDuckGo's bot check. It is left unanswered.
    if response.status_code in {202, 403, 429} or "anomaly" in response.text[:2000].lower():
        raise _EngineBlocked("DuckDuckGo showed a bot check (HTTP %d)" % response.status_code)
    response.raise_for_status()
    return response.text


async def _search_lite_html(
    client: httpx.AsyncClient,
    query: str,
    *,
    page: int = 1,
    time_range: str | None = None,
) -> list[dict[str, Any]]:
    """DuckDuckGo Lite: the full query, with its time filter and next pages."""

    fields = {"q": query.strip()}
    if time_range in _DDG_TIME_FILTERS:
        fields["df"] = _DDG_TIME_FILTERS[time_range]
    html = await _ddg_lite_page(client, fields)
    for _ in range(min(page, _MAX_DDG_PAGE) - 1):
        next_fields = _ddg_next_page_fields(html)
        if next_fields is None:
            return []
        html = await _ddg_lite_page(client, {**fields, **next_fields})
    return _results_from_ddg_lite(html)


_RSS_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)


def _rss_field(item: str, name: str) -> str:
    match = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (name, name), item, re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    value = match.group(1).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3]
    return value


def _site_operators(query: str) -> list[str]:
    return [site.lower() for site in re.findall(r"(?<!\S)site:(\S+)", query, flags=re.IGNORECASE)]


def _on_site(url: str, sites: list[str]) -> bool:
    host = _normalized_host(urlparse(url).hostname)
    return any(host == _normalized_host(site) or host.endswith("." + _normalized_host(site)) for site in sites)


async def _search_bing_rss(
    client: httpx.AsyncClient,
    query: str,
    *,
    page: int = 1,
    time_range: str | None = None,
) -> list[dict[str, Any]]:
    """Bing's RSS feed. Plain ranking only: it ignores operators, dates and paging."""

    del page, time_range
    sites = _site_operators(query)
    plain_query = re.sub(r"(?<!\S)site:(\S+)", r"\1", query, flags=re.IGNORECASE).strip()
    response = await client.get(
        _BING_SEARCH_URL,
        params={"q": plain_query, "format": "rss"},
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
    )
    if response.status_code in {403, 429}:
        raise _EngineBlocked("Bing refused the search (HTTP %d)" % response.status_code)
    response.raise_for_status()
    results: list[dict[str, Any]] = []
    for item in _RSS_ITEM_RE.findall(response.text):
        url = _clean_text(_rss_field(item, "link"))
        if not url:
            continue
        results.append(
            {
                "title": _clean_text(_rss_field(item, "title")),
                "snippet": _clean_text(_rss_field(item, "description"), limit=600),
                "url": url,
                "source": "bing",
            }
        )
    # The feed ignores site:, so keep what is on the site. When nothing is, keep
    # the results anyway: ``site_filter_met`` tells the model they are not.
    on_site = [item for item in results if _on_site(str(item["url"]), sites)]
    return on_site or results


async def _search_google_news_rss(
    client: httpx.AsyncClient,
    query: str,
    *,
    page: int = 1,
    time_range: str | None = None,
) -> list[dict[str, Any]]:
    """Google News' RSS feed: dated headlines. Its links open in a browser, not web_read."""

    del page
    when = _GOOGLE_NEWS_WHEN.get(time_range or _NEWS_DEFAULT_TIME_RANGE)
    response = await client.get(
        _GOOGLE_NEWS_RSS_URL,
        params={"q": "%s when:%s" % (query.strip(), when), "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
    )
    if response.status_code in {403, 429}:
        raise _EngineBlocked("Google News refused the search (HTTP %d)" % response.status_code)
    response.raise_for_status()
    results: list[dict[str, Any]] = []
    for item in _RSS_ITEM_RE.findall(response.text)[:20]:
        url = _clean_text(_rss_field(item, "link"))
        title = _clean_text(_rss_field(item, "title"))
        if not url or not title:
            continue
        result: dict[str, Any] = {
            "title": title,
            "snippet": title,
            "url": url,
            "source": "google_news",
            "open_with": "browser",
        }
        published = _clean_text(_rss_field(item, "pubDate"))
        if published:
            result["publishedDate"] = published
        results.append(result)
    return results


_EngineSearch = Callable[..., Awaitable[list[dict[str, Any]]]]


def _direct_engines(category: str) -> list[tuple[str, _EngineSearch]]:
    engines: list[tuple[str, _EngineSearch]] = [("duckduckgo", _search_lite_html), ("bing", _search_bing_rss)]
    if category == "news":
        # A date filter on the web still ranks home pages first; this is news.
        engines.insert(0, ("google_news", _search_google_news_rss))
    return engines


async def _search_direct_engines(
    client: httpx.AsyncClient,
    query: str,
    category: str,
    *,
    page: int,
    time_range: str | None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Ask the engines in order until one has results.

    Returns the engine that answered, its results, and the engines it stood in
    for. Raises the last failure when every engine failed.
    """
    skipped: list[str] = []
    last_error: Exception | None = None
    answered = ""
    for engine, search in _direct_engines(category):
        if _engine_cooling_down(engine):
            skipped.append(engine)
            continue
        try:
            results = await search(client, query, page=page, time_range=time_range)
        except _EngineBlocked as exc:
            # Never answered, never retried: move on, like moving to another store.
            _engine_cooldowns[engine] = time.monotonic() + _ENGINE_COOLDOWN_SECONDS
            logger.warning("Web search engine %s is unavailable for now: %s", engine, exc)
            last_error = exc
            skipped.append(engine)
            continue
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Web search engine %s failed: %s", engine, type(exc).__name__)
            last_error = exc
            skipped.append(engine)
            continue
        answered = engine
        if results:
            return engine, results, skipped
        skipped.append(engine)
    if answered or last_error is None:
        return answered or "duckduckgo", [], skipped
    raise last_error


async def _instant_answers(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """A short abstract for the query, if there is one. Never fails the search."""
    plain = re.sub(r"(?<!\S)site:\S+\s*", "", query).replace('"', "").strip()
    if not plain:
        return []
    try:
        return await _search_instant_answer(client, plain)
    except Exception as exc:  # noqa: BLE001, RUF100 - the abstract is optional
        logger.debug("DuckDuckGo instant answer unavailable: %s", type(exc).__name__)
        return []


async def web_search(
    query: str,
    category: str = _DEFAULT_CATEGORY,
    page: int | str | None = 1,
    time_range: str | None = None,
    result_limit: int | str | None = None,
) -> ToolResult:
    """Search the public web.

    SearXNG when ``VIOLA_SEARXNG_URL`` is configured (the cloud), otherwise the
    search engines directly (a desktop; see the module docstring). A configured
    SearXNG that fails falls back to those engines and says so as ``degraded``.
    """

    if not query or not query.strip():
        return ToolResult(ok=False, error="Empty search query")

    normalized_query = query.strip()
    normalized_category = _normalize_category(category)
    normalized_page = _normalize_page(page)
    normalized_time = _normalize_time_range(time_range)
    if result_limit is not None:
        logger.debug(
            "Ignoring web_search result_limit=%r; returning all backend candidates",
            result_limit,
        )

    try:
        from admin.instrumentation import record_feature_used

        record_feature_used("web_search")
    except Exception:
        logger.debug("Telemetry record_feature_used (web_search) failed, continuing")

    searxng_url = _searxng_base_url()
    cache_key = _web_search_cache_key(
        query=normalized_query,
        category=normalized_category,
        page=normalized_page,
        time_range=normalized_time,
        searxng_url=searxng_url,
    )
    cached_result = _cached_web_search_result(cache_key, normalized_query)
    if cached_result is not None:
        return cached_result

    searxng_failure_reason: str | None = None
    try:
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            if searxng_url:
                try:
                    bundle = await _search_searxng(
                        client,
                        normalized_query,
                        normalized_category,
                        page=normalized_page,
                        time_range=normalized_time,
                    )
                    raw_results = bundle["results"]
                    results = await _post_process_results(client, raw_results)
                    metadata = bundle["metadata"]
                    result_refs = _with_result_refs(results)
                    data: dict[str, Any] = {
                        "query": normalized_query,
                    }
                    data.update(
                        {
                            "backend": "searxng",
                            "selection_mode": "raw",
                            "enrichment_mode": "generic_trust_signals",
                            "category": metadata.get("category", normalized_category),
                            "page": metadata.get("page", normalized_page),
                            "time_range": metadata.get("time_range"),
                            "raw_candidate_count": len(raw_results),
                            "candidate_count": len(results),
                            "count": len(result_refs),
                            "results": result_refs,
                            "search_metadata": metadata,
                        }
                    )
                    _cache_web_search_result(cache_key, data)
                    return ToolResult(ok=True, data=data)
                except (
                    httpx.HTTPStatusError,
                    httpx.TransportError,
                    httpx.TimeoutException,
                ) as searxng_exc:
                    # API-first triage: SearXNG is preferred, but if it fails we
                    # degrade to the direct engines before giving up.
                    status = getattr(getattr(searxng_exc, "response", None), "status_code", None)
                    searxng_failure_reason = (
                        "SearXNG returned HTTP %d; degraded to direct search." % status
                        if status is not None
                        else "SearXNG unreachable (%s); degraded to direct search." % type(searxng_exc).__name__
                    )
                    logger.warning(
                        "SearXNG web search backend failed: status=%s type=%s; falling back to direct search",
                        status,
                        type(searxng_exc).__name__,
                    )

            served_time = normalized_time
            if normalized_category == "news" and served_time is None:
                served_time = _NEWS_DEFAULT_TIME_RANGE
            wants_abstract = normalized_page == 1 and normalized_time is None and normalized_category != "news"
            abstract_task = asyncio.ensure_future(_instant_answers(client, normalized_query)) if wants_abstract else None
            try:
                engine, results, stood_in_for = await _search_direct_engines(
                    client,
                    normalized_query,
                    normalized_category,
                    page=normalized_page,
                    time_range=served_time,
                )
            except BaseException:
                if abstract_task is not None:
                    abstract_task.cancel()
                raise
            abstracts = await abstract_task if abstract_task is not None else []
            if engine != "duckduckgo":
                # These feeds take the query alone: no next pages, no date filter.
                served_page = 1
                served_time = served_time if engine == "google_news" else None
            else:
                served_page = normalized_page
            raw_candidate_count = len(results) + len(abstracts)
            results = await _post_process_results(client, abstracts + results)
            result_refs = _with_result_refs(results)
            data = {
                "query": normalized_query,
            }
            data.update(
                {
                    "backend": engine,
                    "selection_mode": "raw",
                    "enrichment_mode": "generic_trust_signals",
                    "category": normalized_category,
                    "page": served_page,
                    "time_range": served_time,
                    "raw_candidate_count": raw_candidate_count,
                    "candidate_count": len(results),
                    "count": len(result_refs),
                    "results": result_refs,
                    "search_metadata": {
                        "answers": [str(item.get("snippet") or "") for item in abstracts],
                        "corrections": [],
                        "infoboxes": [],
                        "suggestions": [],
                        "unresponsive_engines": stood_in_for,
                        "number_of_results": None,
                        "page": served_page,
                        "category": normalized_category,
                        "time_range": served_time,
                    },
                }
            )
            sites = _site_operators(normalized_query)
            if sites:
                data["search_metadata"]["site_filter_met"] = bool(results) and all(
                    _on_site(str(item.get("url") or ""), sites) for item in results
                )
            if searxng_failure_reason:
                data["degraded"] = True
                data["degraded_reason"] = searxng_failure_reason
            _cache_web_search_result(cache_key, data)
            return ToolResult(ok=True, data=data)

    except httpx.TimeoutException:
        return ToolResult(
            ok=False,
            error="Web search timed out after %.0f seconds" % _SEARCH_TIMEOUT,
            retryable=True,
        )
    except httpx.HTTPStatusError as exc:
        backend = "searxng" if searxng_url else "web"
        return ToolResult(
            ok=False,
            error="%s search request failed: HTTP %d" % (backend, exc.response.status_code),
            retryable=exc.response.status_code >= 500,
        )
    except _EngineBlocked:
        return ToolResult(
            ok=False,
            error="The search engines are refusing automated searches from this connection right now.",
            retryable=True,
        )
    except httpx.TransportError:
        return ToolResult(
            ok=False,
            error="Web search could not reach any search engine.",
            retryable=True,
        )
    except Exception as exc:
        logger.warning("Web search backend failed: %s", type(exc).__name__)
        return ToolResult(
            ok=False,
            error="Search failed: %s" % exc,
            retryable=True,
        )


__all__ = [
    "_normalize_category",
    "_search_searxng",
    "clear_web_search_counter",
    "notify_browser_navigate",
    "web_search",
]
