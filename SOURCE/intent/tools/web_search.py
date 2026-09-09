"""SearXNG-backed web search tool for the agent.

``web_search`` returns raw discovery snippets with stable result ids. ``web_read``
can read selected public result URLs; browser tools handle JavaScript-heavy,
login, commerce, or interactive pages.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
_DEFAULT_CATEGORY = "general"
_VALID_TIME_RANGES: frozenset[str] = frozenset({"day", "week", "month", "year"})
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
    age_results = await asyncio.gather(
        *(_fetch_domain_age_days(client, domain) for domain in domains),
        return_exceptions=True,
    )
    for domain, age in zip(domains, age_results, strict=False):
        if isinstance(age, int):
            for item in domain_to_items[domain]:
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
        "web_search:v4",
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
    # #2776: a degraded bundle (SearXNG down/unconfigured, DuckDuckGo
    # fallback) must never be cached at the normal success TTL. Caching it
    # would return the same degraded/possibly-empty result as "success" for
    # the full TTL window even after the primary backend recovers.
    if data.get("degraded"):
        logger.debug("Not caching degraded web_search result for key=%s", cache_key)
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
    """Fallback DuckDuckGo Instant Answer search when SearXNG is absent."""

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

    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict) and topic.get("Text"):
            results.append(
                {
                    "title": _clean_text(topic.get("Text"), limit=120),
                    "snippet": _clean_text(topic.get("Text"), limit=600),
                    "url": _clean_text(topic.get("FirstURL")),
                    "source": "duckduckgo",
                }
            )

    return results


async def _search_lite_html(
    client: httpx.AsyncClient,
    query: str,
) -> list[dict[str, Any]]:
    """Fallback DuckDuckGo Lite HTML search when SearXNG is absent."""

    response = await client.post(
        _DDG_LITE_URL,
        data={"q": query.strip()},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
        },
    )
    response.raise_for_status()
    if response.status_code == 202 or "challenge" in response.text[:500].lower():
        raise httpx.HTTPStatusError(
            "DuckDuckGo rate-limited (CAPTCHA)",
            request=response.request,
            response=response,
        )
    html = response.text

    link_pattern = re.compile(
        r"""<a[^>]+class=['"]result-link['"][^>]*href=['"]([^'"]+)['"][^>]*>([^<]+)</a>"""
        r"""|<a[^>]+href=['"]([^'"]+)['"][^>]*class=['"]result-link['"][^>]*>([^<]+)</a>""",
        re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r"""<td[^>]+class=['"]result-snippet['"][^>]*>(.*?)</td>""",
        re.IGNORECASE | re.DOTALL,
    )

    raw_links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)
    links: list[tuple[str, str]] = []
    for match in raw_links:
        url = match[0] or match[2]
        title = match[1] or match[3]
        if url and title:
            links.append((url, title))

    results: list[dict[str, Any]] = []
    for index, (url, title) in enumerate(links):
        snippet = ""
        if index < len(snippets):
            snippet = _clean_text(snippets[index], limit=600)
        results.append(
            {
                "title": _clean_text(title),
                "snippet": snippet,
                "url": _clean_text(url),
                "source": "duckduckgo",
            }
        )

    return results


async def web_search(
    query: str,
    category: str = _DEFAULT_CATEGORY,
    page: int | str | None = 1,
    time_range: str | None = None,
    result_limit: int | str | None = None,
) -> ToolResult:
    """Search the public web.

    SearXNG is the primary backend when configured. DuckDuckGo is retained only
    as a degraded compatibility fallback for local/dev environments that do not
    have ``VIOLA_SEARXNG_URL`` configured.
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
                    # degrade to DuckDuckGo before giving up.
                    status = getattr(getattr(searxng_exc, "response", None), "status_code", None)
                    searxng_failure_reason = (
                        "SearXNG returned HTTP %d; degraded to DuckDuckGo." % status
                        if status is not None
                        else "SearXNG unreachable (%s); degraded to DuckDuckGo." % type(searxng_exc).__name__
                    )
                    logger.warning(
                        "SearXNG web search backend failed: status=%s type=%s; falling back to DuckDuckGo",
                        status,
                        type(searxng_exc).__name__,
                    )

            ddg_query = normalized_query
            ddg_query = re.sub(r"site:\S+\s*", "", ddg_query).strip()
            if ddg_query.count('"') >= 4:
                ddg_query = ddg_query.replace('"', "")
            if not ddg_query:
                ddg_query = normalized_query.replace('"', "")
            results = await _search_instant_answer(client, ddg_query)
            if not results:
                results = await _search_lite_html(client, ddg_query)
            raw_candidate_count = len(results)
            results = await _post_process_results(client, results)
            result_refs = _with_result_refs(results)
            data = {
                "query": ddg_query,
            }
            data.update(
                {
                    "backend": "duckduckgo",
                    "selection_mode": "raw",
                    "enrichment_mode": "generic_trust_signals",
                    "degraded": True,
                    "degraded_reason": (
                        searxng_failure_reason
                        if searxng_failure_reason
                        else "SearXNG is not configured; using legacy DuckDuckGo fallback."
                    ),
                    "category": normalized_category,
                    "page": 1,
                    "raw_candidate_count": raw_candidate_count,
                    "candidate_count": len(results),
                    "count": len(result_refs),
                    "results": result_refs,
                    "search_metadata": {
                        "answers": [],
                        "corrections": [],
                        "infoboxes": [],
                        "suggestions": [],
                        "unresponsive_engines": [],
                        "number_of_results": None,
                        "page": 1,
                        "category": normalized_category,
                        "time_range": None,
                    },
                }
            )
            _cache_web_search_result(cache_key, data)
            return ToolResult(ok=True, data=data)

    except httpx.TimeoutException:
        return ToolResult(
            ok=False,
            error="Web search timed out after %.0f seconds" % _SEARCH_TIMEOUT,
            retryable=True,
        )
    except httpx.HTTPStatusError as exc:
        backend = "searxng" if searxng_url else "duckduckgo"
        return ToolResult(
            ok=False,
            error="%s search request failed: HTTP %d" % (backend, exc.response.status_code),
            retryable=exc.response.status_code >= 500,
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
