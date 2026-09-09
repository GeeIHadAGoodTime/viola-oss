"""Clean web page reader for search-result follow-up.

Fetches a URL directly over HTTP and returns cleaned main-page text plus
lightweight metadata. This gives the model a non-browser path between
search snippets and full interactive browser control.
"""

from __future__ import annotations

import datetime
import re
import time
from html import escape, unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - exercised via live runtime fallback
    BeautifulSoup = None

from core.logging_config import get_logger
from core.url_validation import validate_external_url
from intent.tool_types import ToolResult

logger = get_logger(__name__)

_READ_TIMEOUT = 15.0
_CACHE_TTL_SECONDS = 30 * 60
_MAX_REDIRECTS = 10
_URL_REFUSED_ERROR = "URL refused: private/loopback/metadata range"
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
_NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "form",
    "nav",
    "aside",
    "footer",
    "button",
    "input",
)
_NOISE_CLASS_RE = re.compile(
    r"(cookie|consent|promo|advert|ads|share|social|newsletter|subscribe|signup|"
    r"sign-up|breadcrumb|sidebar|footer|header|menu|modal|popup)",
    re.IGNORECASE,
)

# Elements with visible text matching this pattern are effectively empty and
# benefit from hydration with attribute-backed values (e.g., "--" placeholders
# on sports scoreboards where the real number lives in data-score).
_PLACEHOLDER_TEXT_RE = re.compile(r"^[\s\-\u2013\u2014\u00b7\u2022.]{0,6}$")

# Attributes that commonly hold user-visible state rendered via JS. Order
# matters: numeric/structural first, then aria-label as a last resort.
_HYDRATE_VALUE_ATTRS = (
    "data-score",
    "data-value",
    "data-result",
    "data-number",
    "data-stat",
    "data-count",
    "data-points",
    "data-time",
    "data-price",
    "data-amount",
)


def _normalize_url(url: str) -> str:
    return re.sub(r"\s+", " ", (url or "").strip())


def _valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class _WebReadUrlRefused(ValueError):
    """Raised when web_read is asked to fetch an internal URL."""


def _ensure_external_fetch_url(url: str) -> str:
    try:
        return validate_external_url(url)
    except ValueError as exc:
        raise _WebReadUrlRefused(_URL_REFUSED_ERROR) from exc


async def _get_validated_response(client: httpx.AsyncClient, url: str) -> httpx.Response:
    current_url = _ensure_external_fetch_url(url)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        response = await client.get(current_url)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code not in _REDIRECT_STATUS_CODES:
            return response

        location = response.headers.get("location", "")
        if not location:
            return response
        if redirect_count >= _MAX_REDIRECTS:
            raise httpx.TooManyRedirects(
                "Exceeded maximum redirects",
                request=getattr(response, "request", None),
            )
        current_url = _ensure_external_fetch_url(urljoin(str(response.url), location))

    raise httpx.TooManyRedirects("Exceeded maximum redirects")


def _read_limits(max_chars: int | None) -> tuple[int, int]:
    from config.settings import settings

    default_chars = getattr(settings, "web_read_default_chars", 12000)
    max_allowed = getattr(settings, "web_read_max_chars", 20000)
    try:
        default_chars = max(1000, int(default_chars))
    except (TypeError, ValueError):
        default_chars = 12000
    try:
        max_allowed = max(default_chars, int(max_allowed))
    except (TypeError, ValueError):
        max_allowed = 20000
    if max_chars is None:
        return default_chars, max_allowed
    try:
        requested = int(max_chars)
    except (TypeError, ValueError):
        requested = default_chars
    requested = max(500, requested)
    return min(requested, max_allowed), max_allowed


def _meta_content(soup: BeautifulSoup, *, names: tuple[str, ...] = (), props: tuple[str, ...] = ()) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    for prop in props:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _collapse_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    filtered = [line for line in lines if line]
    return "\n".join(filtered)


def _hydrate_attribute_values(root: BeautifulSoup | Any) -> None:
    """Inject attribute-backed values into elements whose visible text is empty.

    Many modern pages (NFL.com scoreboards, shopping cards, stats widgets) render
    numeric state into ``data-*`` attributes and leave the visible text as a
    placeholder like ``"--"`` until client-side JS hydrates. A server-side HTTP
    fetch never runs that JS, so ``get_text()`` returns the placeholder and the
    reader loses the value the user actually cares about.

    This helper walks the DOM and replaces empty/placeholder text nodes with
    their ``data-score`` / ``data-value`` / ``aria-label`` (etc.) value when
    such an attribute exists. Elements that already have meaningful visible
    text are left alone so we don't duplicate content.

    Safe for lossy HTML: ignores nodes without ``attrs``, missing ``clear()``,
    or non-string attribute values.
    """
    for node in list(root.find_all(True)):
        attrs = getattr(node, "attrs", None)
        if not attrs:
            continue
        try:
            current_text = node.get_text("", strip=True)
        except Exception:
            continue
        if current_text and not _PLACEHOLDER_TEXT_RE.match(current_text):
            continue

        hydrated = ""
        for attr_name in _HYDRATE_VALUE_ATTRS:
            raw = attrs.get(attr_name)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                hydrated = value
                break

        if not hydrated:
            aria = attrs.get("aria-label")
            if aria:
                aria_value = str(aria).strip()
                # aria-label often duplicates visible text for a11y. Only use
                # it when visible text is actually empty/placeholder (already
                # checked above) AND the aria text carries digits or is short.
                if aria_value and (any(ch.isdigit() for ch in aria_value) or len(aria_value) <= 80):
                    hydrated = aria_value

        if not hydrated:
            continue

        try:
            node.clear()
            node.append(hydrated)
        except Exception:
            # Non-mutable node (e.g. NavigableString parent); skip silently.
            continue


def _parse_html_attrs_fallback(attrs_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", attrs_text, re.DOTALL):
        attrs[match.group(1).lower()] = unescape(match.group(3)).strip()
    return attrs


def _visible_text_from_html_fragment(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return _collapse_text(unescape(text)).replace("\n", " ").strip()


def _hydrated_value_from_attrs(attrs: dict[str, str], visible_text: str) -> str:
    if visible_text and not _PLACEHOLDER_TEXT_RE.match(visible_text):
        return ""

    for attr_name in _HYDRATE_VALUE_ATTRS:
        value = attrs.get(attr_name)
        if value:
            return value

    aria_value = attrs.get("aria-label", "")
    if aria_value and (any(ch.isdigit() for ch in aria_value) or len(aria_value) <= 80):
        return aria_value
    return ""


def _hydrate_html_segment_fallback(segment: str) -> str:
    """Regex fallback for attribute hydration when BeautifulSoup is unavailable."""

    def _replace_leaf(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs_text = match.group("attrs")
        body = match.group("body")
        attrs = _parse_html_attrs_fallback(attrs_text)
        hydrated = _hydrated_value_from_attrs(attrs, _visible_text_from_html_fragment(body))
        if not hydrated:
            return match.group(0)
        return "<%s%s>%s</%s>" % (tag, attrs_text, escape(hydrated), tag)

    return re.sub(
        r"<(?P<tag>span|td|th|b|strong|em|small)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
        _replace_leaf,
        segment,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _prune_noise(root: BeautifulSoup | Any) -> None:
    for tag_name in _NOISE_TAGS:
        for node in list(root.find_all(tag_name)):
            node.decompose()
    for node in list(root.find_all(True)):
        attrs_obj = getattr(node, "attrs", None)
        if attrs_obj is None:
            continue
        attrs = " ".join(
            str(value)
            for value in (
                attrs_obj.get("class", []),
                attrs_obj.get("id", ""),
                attrs_obj.get("role", ""),
                attrs_obj.get("aria-label", ""),
            )
            if value
        )
        if attrs and _NOISE_CLASS_RE.search(attrs):
            node.decompose()


def _choose_content_root(soup: BeautifulSoup) -> tuple[Any, str]:
    for selector, label in (
        ("main", "main"),
        ("article", "article"),
        ("[role='main']", "role_main"),
        ("section", "section"),
    ):
        node = soup.select_one(selector)
        if node is not None:
            return node, label
    return soup.body or soup, "body"


def _slice_text(text: str, offset: int, max_chars: int) -> dict[str, Any]:
    total_chars = len(text)
    offset = max(0, int(offset))
    if offset >= total_chars:
        return {
            "text": "",
            "offset": offset,
            "returned_chars": 0,
            "remaining_chars": 0,
            "next_offset": None,
            "truncated": False,
            "total_chars": total_chars,
        }
    chunk = text[offset : offset + max_chars]
    remaining_chars = max(0, total_chars - (offset + len(chunk)))
    next_offset = offset + len(chunk) if remaining_chars else None
    return {
        "text": chunk,
        "offset": offset,
        "returned_chars": len(chunk),
        "remaining_chars": remaining_chars,
        "next_offset": next_offset,
        "truncated": remaining_chars > 0,
        "total_chars": total_chars,
    }


def _extract_html_payload(html: str, page_url: str) -> dict[str, Any]:
    if BeautifulSoup is None:
        return _extract_html_payload_fallback(html, page_url)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    root, content_source = _choose_content_root(soup)
    # Hydrate attribute-backed values (e.g. data-score="27" on <span>--</span>)
    # BEFORE pruning, so we don't lose values inside nodes the pruner removes.
    _hydrate_attribute_values(root)
    _prune_noise(root)

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    description = _meta_content(
        soup,
        names=("description", "twitter:description"),
        props=("og:description",),
    )
    published_date = _meta_content(
        soup,
        names=("article:published_time", "pubdate", "publishdate"),
        props=("article:published_time", "og:updated_time"),
    )
    canonical_url = ""
    canonical_tag = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
    if canonical_tag and canonical_tag.get("href"):
        canonical_url = str(canonical_tag["href"]).strip()

    text = _collapse_text(root.get_text("\n", strip=True))
    return {
        "title": title,
        "description": description,
        "published_date": published_date,
        "canonical_url": canonical_url,
        "content_source": content_source,
        "text": text,
        "url": page_url,
    }


def _extract_html_payload_fallback(html: str, page_url: str) -> dict[str, Any]:
    """Fallback reader when BeautifulSoup is unavailable in the app runtime."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = unescape(title_match.group(1)).strip() if title_match else ""

    description_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    description = unescape(description_match.group(1)).strip() if description_match else ""

    published_match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|pubdate|publishdate|og:updated_time)["\'][^>]+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    published_date = unescape(published_match.group(1)).strip() if published_match else ""

    canonical_match = re.search(
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    canonical_url = unescape(canonical_match.group(1)).strip() if canonical_match else ""

    segment = html
    for pattern, label in (
        (r"<main\b[^>]*>(.*?)</main>", "main_fallback"),
        (r"<article\b[^>]*>(.*?)</article>", "article_fallback"),
        (r'<[^>]+\brole=["\']main["\'][^>]*>(.*?)</[^>]+>', "role_main_fallback"),
    ):
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            segment = match.group(1)
            content_source = label
            break
    else:
        content_source = "body_fallback"

    segment = _hydrate_html_segment_fallback(segment)
    cleaned = re.sub(r"<!--.*?-->", " ", segment, flags=re.DOTALL)
    cleaned = re.sub(
        r"<(script|style|noscript|svg|canvas|iframe|form|nav|aside|footer|header|button|input)[^>]*>.*?</\1>",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"</(p|div|section|article|main|li|ul|ol|h1|h2|h3|h4|h5|h6|tr)>", "\n", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    text = _collapse_text(unescape(cleaned))
    return {
        "title": title,
        "description": description,
        "published_date": published_date,
        "canonical_url": canonical_url,
        "content_source": content_source,
        "text": text,
        "url": page_url,
    }


def _cache_age_seconds(fetched_at_epoch: object) -> int | None:
    """Age of a cached bundle in whole seconds, or None when unknowable.

    Entries written before the stamp existed carry no ``fetched_at_epoch``;
    those report ``cached: true`` with no age rather than an invented one.
    """
    try:
        stamped = float(fetched_at_epoch)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    age = time.time() - stamped
    if age < 0:
        return None
    return int(age)


async def web_read(url: str, max_chars: int | None = None, offset: int = 0) -> ToolResult:
    """Read a web page directly and return cleaned main content text."""
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return ToolResult(ok=False, error="URL is required")
    if not _valid_http_url(normalized_url):
        return ToolResult(ok=False, error="web_read only supports http/https URLs")
    try:
        normalized_url = _ensure_external_fetch_url(normalized_url)
    except _WebReadUrlRefused:
        return ToolResult(ok=False, error=_URL_REFUSED_ERROR)

    limit, max_allowed = _read_limits(max_chars)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    cache_key = "web_read:%s:%d:%d" % (normalized_url, limit, offset)
    try:
        from services.api_cache import get_api_cache

        cached = get_api_cache().get_public(cache_key)
        if isinstance(cached, dict):
            # A cache hit is a replay of a page fetched up to the full TTL ago.
            # Returned unmarked it was indistinguishable from a live fetch, so
            # the model read a half-hour-old page as the current state of the
            # web and reported it as such. web_search's cached path already
            # marks its bundles; this one now says the same thing.
            replay = dict(cached)
            replay["cached"] = True
            age_seconds = _cache_age_seconds(replay.get("fetched_at_epoch"))
            if age_seconds is not None:
                replay["cache_age_seconds"] = age_seconds
            return ToolResult(ok=True, data=replay, truncated=bool(replay.get("truncated")))
    except Exception:
        logger.debug("web_read cache lookup failed, continuing")

    try:
        from admin.instrumentation import record_feature_used

        record_feature_used("web_read")
    except Exception:
        logger.debug("Telemetry record_feature_used (web_read) failed, continuing")

    try:
        async with httpx.AsyncClient(
            timeout=_READ_TIMEOUT,
            follow_redirects=False,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            },
        ) as client:
            response = await _get_validated_response(client, normalized_url)
            response.raise_for_status()
    except _WebReadUrlRefused:
        return ToolResult(ok=False, error=_URL_REFUSED_ERROR)
    except httpx.TooManyRedirects:
        return ToolResult(ok=False, error="web_read failed: too many redirects")
    except httpx.TimeoutException:
        return ToolResult(ok=False, error="web_read timed out after %.0f seconds" % _READ_TIMEOUT)
    except httpx.HTTPStatusError as exc:
        return ToolResult(ok=False, error="web_read failed: HTTP %d" % exc.response.status_code)
    except Exception as exc:
        logger.warning("web_read failed for %s: %s", normalized_url[:120], exc)
        return ToolResult(ok=False, error="web_read failed: %s" % exc)

    content_type = (response.headers.get("content-type", "") or "").lower()
    final_url = str(response.url)
    if "html" in content_type or "<html" in response.text[:500].lower():
        payload = _extract_html_payload(response.text, final_url)
    elif content_type.startswith("text/"):
        payload = {
            "title": "",
            "description": "",
            "published_date": "",
            "canonical_url": "",
            "content_source": "text",
            "text": _collapse_text(response.text),
            "url": final_url,
        }
    else:
        return ToolResult(
            ok=False,
            error="web_read cannot parse content-type '%s'" % (content_type or "unknown"),
        )

    fetched_at = datetime.datetime.now(datetime.UTC)
    text_slice = _slice_text(payload["text"], offset, limit)
    data = {
        "url": normalized_url,
        "final_url": final_url,
        "title": payload["title"],
        "description": payload["description"],
        "published_date": payload["published_date"],
        "canonical_url": payload["canonical_url"],
        "content_source": payload["content_source"],
        "content_type": content_type or "text/html",
        "max_chars": limit,
        "max_chars_cap": max_allowed,
        "cached": False,
        "fetched_at": fetched_at.isoformat(),
        # Wall-clock stamp so a later cache hit can report how old the bundle
        # is. The public cache is durable (SQLite), so this must survive a
        # restart -- a monotonic clock would not.
        "fetched_at_epoch": fetched_at.timestamp(),
        **text_slice,
    }
    try:
        from services.api_cache import get_api_cache

        get_api_cache().set_public(cache_key, data, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception:
        logger.debug("web_read cache write failed, continuing")
    return ToolResult(ok=True, data=data, truncated=bool(data["truncated"]))


__all__ = ["web_read"]
