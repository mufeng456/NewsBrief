"""Source-backed news verification assistance.

The module reports evidence states for claims extracted from a supplied news
article. It deliberately does not decide whether a news item is true or false.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .facts import extract_facts
from .summarizer import analyze_news, split_sentences, tokenize

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
MAX_QUERIES = 3
MAX_SEARCH_RESULTS = 8
MAX_SOURCES = 6
MAX_REDIRECTS = 3
MAX_SOURCE_BYTES = 1_500_000
SOURCE_TIMEOUT = 6.0
VERIFICATION_TIMEOUT = 20.0
EMBEDDED_CONTENTDATE_PATTERN = re.compile(
    r"(?:var|let|const)\s+contentdate\s*=\s*'(?P<content>(?:\\.|[^'])*)'\s*;?",
    re.IGNORECASE | re.DOTALL,
)
EMBEDDED_CONTENTDATE_DOUBLE_QUOTE_PATTERN = re.compile(
    r'(?:var|let|const)\s+contentdate\s*=\s*"(?P<content>(?:\\.|[^\"])*)"\s*;?',
    re.IGNORECASE | re.DOTALL,
)
META_CHARSET_PATTERN = re.compile(
    br"<meta[^>]+charset=[\"']?([a-zA-Z0-9_-]+)", re.IGNORECASE
)
CONTENT_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    "#artibody",
    "#articleContent",
    "#article-content",
    "#article_content",
    "#content_area",
    "#contentText",
    "#content-text",
    "#main-content",
    "#mainContent",
    "#UCAP-CONTENT",
    "#Cnt-Main-Article-QQ",
    "#endText",
    "#ozoom",
    ".article-content",
    ".articleContent",
    ".article_body",
    ".article-body",
    ".articleBody",
    ".article_txt",
    ".article-text",
    ".articleText",
    ".article-main",
    ".content-article",
    ".content-detail",
    ".content-main",
    ".news-content",
    ".newsContent",
    ".news_detail",
    ".TRS_Editor",
    ".post_content",
    ".rich_media_content",
)
CONTENT_NOISE_PATTERN = re.compile(
    r"^(?:责任编辑|编辑|原标题|来源|版权声明|特别声明|更多精彩|相关阅读|推荐阅读|点击进入|"
    r"本文转自|扫码|分享至|客户端|下载客户端|举报|收藏|打印|关闭|返回顶部)[:：\s]",
    re.IGNORECASE,
)
ARTICLE_SCHEMA_TYPES = {"article", "newsarticle", "reportage", "blogposting"}
CACHE_SECONDS = 600

ESTABLISHED_MEDIA_DOMAINS = {
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "chinanews.com.cn",
}
FACT_PRIORITY = ("event", "subject", "time", "number", "location", "impact")
NUMERIC_PATTERN = re.compile(
    r"\d{1,4}(?:\.\d+)?(?:%|亿元|万元|元|人|名|户|项|次|辆|公里|小时|分钟|天|月|年)?"
)
TIME_PATTERN = re.compile(
    r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}[：:]\d{2}|"
    r"今日|近日|当天|今年|明年|本周|下周|未来)"
)

Resolver = Callable[[str], Awaitable[list[str]]]
Fetcher = Callable[[httpx.AsyncClient, str], Awaitable[dict[str, Any] | None]]
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_network_slots = asyncio.Semaphore(2)


class VerificationError(RuntimeError):
    """Raised for safe, user-facing verification failures."""


def _claim_id(kind: str, source_ids: list[int]) -> str:
    source_id = source_ids[0] if source_ids else 0
    return f"{kind}-{source_id}"


def build_offline_verification(
    facts: list[dict[str, Any]], source_sentences: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build local, source-traceable claims without making a network request."""
    source_ids = {int(sentence["id"]) for sentence in source_sentences}
    fact_by_kind = {fact["kind"]: fact for fact in facts}
    claims: list[dict[str, Any]] = []
    for kind in FACT_PRIORITY:
        fact = fact_by_kind.get(kind)
        if not fact:
            continue
        evidence_ids = [int(item) for item in fact["evidence_sentence_ids"]]
        if not evidence_ids or not set(evidence_ids).issubset(source_ids):
            continue
        claims.append(
            {
                "id": _claim_id(kind, evidence_ids),
                "kind": kind,
                "label": fact["label"],
                "text": fact["value"],
                "source_sentence_ids": evidence_ids,
                "status": "offline_only",
                "evidence_source_ids": [],
            }
        )
        if len(claims) == 5:
            break
    return {
        "mode": "offline",
        "status": "unavailable",
        "claims": claims,
        "sources": [],
        "searched_at": None,
        "notice": "尚未联网检索。证据状态不等同于新闻真实性结论。",
    }


def offline_verification_for_article(title: str, content: str) -> dict[str, Any]:
    analysis = analyze_news(title, content)
    facts = extract_facts(analysis)
    source_sentences = [
        {"id": index, "text": sentence} for index, sentence in enumerate(analysis.raw_sentences)
    ]
    return build_offline_verification(facts, source_sentences)


def _query_terms(text: str) -> list[str]:
    return [term for term in tokenize(text) if len(term) > 1][:12]


def build_queries(title: str, claims: list[dict[str, Any]]) -> list[str]:
    """Generate a small set of short, factual queries rather than sending the article."""
    event = next((claim for claim in claims if claim["kind"] == "event"), None)
    subject = next((claim for claim in claims if claim["kind"] == "subject"), None)
    time_or_number = [
        claim for claim in claims if claim["kind"] in {"time", "number", "location"}
    ]
    base = " ".join(_query_terms(title)[:8]) or " ".join(
        _query_terms(event["text"] if event else "")[:8]
    )
    if subject:
        base = " ".join(dict.fromkeys(_query_terms(subject["text"])[:5] + base.split()))
    queries = [base.strip()] if base.strip() else []
    for claim in time_or_number:
        query = " ".join(
            dict.fromkeys((base.split()[:6] if base else []) + _query_terms(claim["text"])[:5])
        ).strip()
        if query and query not in queries:
            queries.append(query)
        if len(queries) == MAX_QUERIES:
            break
    return queries[:MAX_QUERIES]


async def _resolve_public_addresses(hostname: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in infos})


async def is_safe_public_url(url: str, resolver: Resolver = _resolve_public_addresses) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = await resolver(hostname)
    except (OSError, socket.gaierror):
        return False
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        return False


def source_tier(domain: str) -> str:
    normalized = domain.lower().lstrip("www.")
    if normalized == "gov.cn" or normalized.endswith(".gov.cn"):
        return "official"
    if normalized == "edu.cn" or normalized.endswith(".edu.cn"):
        return "official"
    if any(normalized == item or normalized.endswith(f".{item}") for item in ESTABLISHED_MEDIA_DOMAINS):
        return "established_media"
    return "other"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _deduplicate_paragraphs(paragraphs: list[str], minimum_length: int = 14) -> list[str]:
    """Keep readable article paragraphs while dropping common page chrome."""
    selected: list[str] = []
    seen: set[str] = set()
    for raw_paragraph in paragraphs:
        paragraph = _clean_text(raw_paragraph)
        fingerprint = re.sub(r"[\W_]+", "", paragraph)
        if (
            len(paragraph) < minimum_length
            or len(fingerprint) < minimum_length
            or "htmlVideoCode" in paragraph
            or CONTENT_NOISE_PATTERN.search(paragraph)
            or fingerprint in seen
        ):
            continue
        seen.add(fingerprint)
        selected.append(paragraph)
    return selected


def _paragraphs_from_node(node: Any, minimum_length: int = 14) -> list[str]:
    paragraph_tags = node.find_all("p")
    if paragraph_tags:
        values = [paragraph.get_text(" ", strip=True) for paragraph in paragraph_tags]
    else:
        values = node.get_text("\n", strip=True).splitlines()
    return _deduplicate_paragraphs(values, minimum_length)


def _unescape_script_html(value: str) -> str:
    """Decode common JavaScript string escapes without evaluating any script."""

    def decode_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    value = re.sub(r"\\u([0-9a-fA-F]{4})", decode_unicode, value)
    value = re.sub(r"\\x([0-9a-fA-F]{2})", decode_unicode, value)
    return (
        value.replace(r"\'", "'")
        .replace(r'\"', '"')
        .replace(r"\/", "/")
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", " ")
    )


def _embedded_contentdate_paragraphs(html: str) -> list[str]:
    """Read CCTV-style inline article HTML without executing page scripts."""
    for pattern in (EMBEDDED_CONTENTDATE_PATTERN, EMBEDDED_CONTENTDATE_DOUBLE_QUOTE_PATTERN):
        match = pattern.search(html)
        if match:
            content_html = _unescape_script_html(match.group("content"))
            return _paragraphs_from_node(BeautifulSoup(content_html, "html.parser"))
    return []


def _paragraphs_from_article_value(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    fragment = BeautifulSoup(value, "html.parser")
    paragraphs = _paragraphs_from_node(fragment)
    if paragraphs:
        return paragraphs
    return _deduplicate_paragraphs(value.splitlines())


def _walk_json_objects(payload: Any) -> list[dict[str, Any]]:
    """Bound traversal of JSON-LD and framework state data embedded in a page."""
    objects: list[dict[str, Any]] = []
    pending = [payload]
    while pending and len(objects) < 800:
        current = pending.pop()
        if isinstance(current, dict):
            objects.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current[:80])
    return objects


def _article_schema_types(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {str(item).lower() for item in values if item}


def _json_article_candidates(soup: BeautifulSoup) -> tuple[list[tuple[str, list[str]]], list[str]]:
    candidates: list[tuple[str, list[str]]] = []
    titles: list[str] = []
    for script in soup.find_all("script"):
        script_type = str(script.get("type", "")).lower()
        script_id = str(script.get("id", "")).lower()
        if script_type not in {"application/ld+json", "application/json"} and script_id != "__next_data__":
            continue
        raw_json = script.string or script.get_text(strip=True)
        if not raw_json or len(raw_json) > MAX_SOURCE_BYTES:
            continue
        try:
            # Some publishers leave literal line breaks inside an otherwise usable data block.
            payload = json.loads(raw_json, strict=False)
        except json.JSONDecodeError:
            continue
        for item in _walk_json_objects(payload):
            schema_types = _article_schema_types(item.get("@type") or item.get("type"))
            body = item.get("articleBody") or item.get("article_body")
            if not body and schema_types & ARTICLE_SCHEMA_TYPES:
                body = item.get("content") or item.get("body")
            paragraphs = _paragraphs_from_article_value(body)
            if paragraphs:
                candidates.append(("structured-data", paragraphs))
                headline = _clean_text(str(item.get("headline") or item.get("name") or ""))
                if headline:
                    titles.append(headline)
    return candidates, titles


def _metadata_title(soup: BeautifulSoup) -> str:
    for attribute, name in (
        ("property", "og:title"),
        ("name", "twitter:title"),
        ("name", "parsely-title"),
    ):
        tag = soup.find("meta", attrs={attribute: name})
        if tag and tag.get("content"):
            title = _clean_text(str(tag["content"]))
            if title:
                return title
    heading = soup.find("h1")
    if heading:
        title = _clean_text(heading.get_text(" ", strip=True))
        if title:
            return title
    return ""


def _candidate_score(method: str, paragraphs: list[str]) -> int:
    text_length = len("".join(paragraphs))
    method_priority = {
        "structured-data": 640,
        "embedded-contentdate": 620,
        "semantic-container": 440,
        "page-paragraphs": 0,
    }[method]
    return method_priority + min(text_length // 8, 620) + min(len(paragraphs) * 40, 360)


def _select_article_text(candidates: list[tuple[str, list[str]]]) -> str:
    valid_candidates = [
        (method, paragraphs)
        for method, paragraphs in candidates
        if len("".join(paragraphs)) >= 40
    ]
    if not valid_candidates:
        return ""
    _, paragraphs = max(valid_candidates, key=lambda item: _candidate_score(*item))
    return "\n".join(paragraphs[:120])[:18_000]


def _extract_page(html: str, fallback_title: str) -> tuple[str, str]:
    """Extract a readable article with progressively less specific static strategies."""
    soup = BeautifulSoup(html, "html.parser")
    structured_candidates, structured_titles = _json_article_candidates(soup)
    candidates = structured_candidates
    embedded_paragraphs = _embedded_contentdate_paragraphs(html)
    if embedded_paragraphs:
        candidates.append(("embedded-contentdate", embedded_paragraphs))

    for tag in soup(
        ["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "template"]
    ):
        tag.decompose()
    for selector in CONTENT_SELECTORS:
        for container in soup.select(selector)[:8]:
            paragraphs = _paragraphs_from_node(container)
            if paragraphs:
                candidates.append(("semantic-container", paragraphs))
    page_paragraphs = _paragraphs_from_node(soup, minimum_length=24)
    if page_paragraphs:
        candidates.append(("page-paragraphs", page_paragraphs))

    title = _metadata_title(soup)
    if not title and structured_titles:
        title = structured_titles[0]
    if not title and soup.title:
        title = _clean_text(soup.title.get_text(" ", strip=True))
    text = _select_article_text(candidates)
    if not text:
        text = _clean_text(soup.get_text(" ", strip=True))[:18_000]
    return (title or fallback_title)[:180], text


def _decode_page_html(raw_html: bytes, content_type: str) -> str:
    """Decode common news-page encodings before HTML parsing."""
    header_match = re.search(r"charset\s*=\s*([a-zA-Z0-9_-]+)", content_type, re.IGNORECASE)
    meta_match = META_CHARSET_PATTERN.search(raw_html[:16_000])
    candidates = [
        meta_match.group(1).decode("ascii", errors="ignore") if meta_match else "",
        header_match.group(1) if header_match else "",
        "utf-8",
        "gb18030",
        "big5",
        "latin-1",
    ]
    for encoding in dict.fromkeys(candidate.lower().replace("gb2312", "gb18030") for candidate in candidates):
        if not encoding:
            continue
        try:
            return raw_html.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_html.decode("utf-8", errors="replace")


async def fetch_public_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Resolver = _resolve_public_addresses,
) -> dict[str, Any] | None:
    """Fetch a bounded public HTML page and validate every redirect hop."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await is_safe_public_url(current_url, resolver):
            return None
        try:
            async with client.stream(
                "GET",
                current_url,
                headers={"User-Agent": "NewsBriefVerification/2.1"},
                follow_redirects=False,
                timeout=SOURCE_TIMEOUT,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current_url = str(response.url.join(location))
                    continue
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        return None
                    chunks.append(chunk)
        except (httpx.HTTPError, ValueError):
            return None
        raw_html = _decode_page_html(b"".join(chunks), content_type)
        title, text = _extract_page(raw_html, urlparse(current_url).netloc)
        if len(text) < 40:
            return None
        domain = (urlparse(current_url).hostname or "").lower()
        return {
            "title": title,
            "url": current_url,
            "domain": domain,
            "tier": source_tier(domain),
            "text": text,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return None


class SearchProvider(Protocol):
    """Minimal interface shared by public-web search adapters."""

    name: str

    async def search(
        self, client: httpx.AsyncClient, query: str, api_key: str
    ) -> list[dict[str, str]]: ...


class BochaSearchProvider:
    """Adapter for Bocha's structured Web Search API, the domestic default."""

    name = "bocha"

    async def search(
        self, client: httpx.AsyncClient, query: str, api_key: str
    ) -> list[dict[str, str]]:
        response = await client.post(
            BOCHA_SEARCH_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            json={"query": query, "summary": True, "count": MAX_SEARCH_RESULTS},
            timeout=SOURCE_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", payload)
        results = data.get("webPages", {}).get("value", []) if isinstance(data, dict) else []
        return [
            {
                "title": str(item.get("name") or item.get("title") or "")[:180],
                "url": str(item.get("url") or item.get("id") or ""),
                "description": str(
                    item.get("snippet") or item.get("summary") or item.get("description") or ""
                )[:500],
            }
            for item in results[:MAX_SEARCH_RESULTS]
            if isinstance(item, dict) and (item.get("url") or item.get("id"))
        ]


class BraveSearchProvider:
    """Small adapter around Brave's documented JSON web-search endpoint."""

    name = "brave"

    async def search(
        self, client: httpx.AsyncClient, query: str, api_key: str
    ) -> list[dict[str, str]]:
        response = await client.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": MAX_SEARCH_RESULTS, "search_lang": "zh-hans"},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=SOURCE_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("web", {}).get("results", [])
        return [
            {
                "title": str(item.get("title", ""))[:180],
                "url": str(item.get("url", "")),
                "description": str(item.get("description", ""))[:500],
            }
            for item in results[:MAX_SEARCH_RESULTS]
            if item.get("url")
        ]


def get_search_provider(provider_name: str) -> SearchProvider:
    providers: dict[str, SearchProvider] = {
        "bocha": BochaSearchProvider(),
        "brave": BraveSearchProvider(),
    }
    try:
        return providers[provider_name.strip().lower()]
    except KeyError as error:
        raise VerificationError("不支持的公开来源检索服务。") from error


def _match_score(claim_text: str, source_text: str) -> float:
    claim_terms = set(_query_terms(claim_text))
    source_terms = set(_query_terms(source_text))
    if not claim_terms or not source_terms:
        return 0.0
    return len(claim_terms & source_terms) / len(claim_terms)


def _has_conflict(kind: str, claim_text: str, source_text: str, relevance: float) -> bool:
    if relevance < 0.32:
        return False
    if kind == "number":
        expected = set(NUMERIC_PATTERN.findall(claim_text))
        observed = set(NUMERIC_PATTERN.findall(source_text))
        return bool(expected and observed and not expected & observed)
    if kind == "time":
        expected = set(TIME_PATTERN.findall(claim_text))
        observed = set(TIME_PATTERN.findall(source_text))
        return bool(expected and observed and not expected & observed)
    return False


def _best_excerpt(claim_text: str, source_text: str) -> str:
    sentences = split_sentences(source_text) or [source_text]
    best = max(sentences, key=lambda sentence: _match_score(claim_text, sentence), default="")
    return best[:280]


def assess_claims(result: dict[str, Any], pages: list[dict[str, Any]]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    claims = copy.deepcopy(result["claims"])
    for index, page in enumerate(pages):
        source_id = f"source-{index + 1}"
        sources.append(
            {
                "id": source_id,
                "title": page["title"],
                "url": page["url"],
                "domain": page["domain"],
                "tier": page["tier"],
                "excerpt": page["text"][:280],
                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "content_sha256": page["content_sha256"],
            }
        )
    for claim in claims:
        supporting: list[tuple[int, float]] = []
        partial: list[tuple[int, float]] = []
        conflicts: list[tuple[int, float]] = []
        for index, page in enumerate(pages):
            if page["tier"] == "other":
                continue
            score = _match_score(claim["text"], page["text"])
            exact = claim["text"] in page["text"]
            if _has_conflict(claim["kind"], claim["text"], page["text"], score):
                conflicts.append((index, score))
            elif exact or score >= 0.62:
                supporting.append((index, score))
            elif score >= 0.32:
                partial.append((index, score))
        matched = conflicts or supporting or partial
        claim["evidence_source_ids"] = [f"source-{index + 1}" for index, _ in matched]
        if conflicts and not supporting:
            claim["status"] = "conflicting"
        elif supporting:
            claim["status"] = "supported"
        elif partial:
            claim["status"] = "partial"
        else:
            claim["status"] = "unverified"
        if matched:
            best_index = max(matched, key=lambda item: item[1])[0]
            sources[best_index]["excerpt"] = _best_excerpt(claim["text"], pages[best_index]["text"])
    result.update(
        {
            "mode": "online",
            "status": "completed" if pages else "partial",
            "claims": claims,
            "sources": sources,
            "searched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "notice": "证据状态不等同于新闻真实性结论，请打开来源原文继续核查。",
        }
    )
    return result


async def run_online_verification(
    title: str,
    content: str,
    api_key: str,
    *,
    provider: SearchProvider | None = None,
    client: httpx.AsyncClient | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Search public sources and return evidence states with safe degradation."""
    offline = offline_verification_for_article(title, content)
    if not api_key.strip():
        return offline
    provider = provider or BochaSearchProvider()
    provider_name = getattr(provider, "name", "custom")
    cache_key = hashlib.sha256(f"{provider_name}\n{title}\n{content}".encode()).hexdigest()
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return copy.deepcopy(cached[1])
    queries = build_queries(title, offline["claims"])
    if not queries:
        offline["notice"] = "无法从当前新闻中生成可用于公开检索的短查询。"
        return offline

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=SOURCE_TIMEOUT)
    fetcher = fetcher or fetch_public_page
    try:
        async with _network_slots:
            candidates: list[dict[str, str]] = []
            seen_urls: set[str] = set()
            try:
                async with asyncio.timeout(VERIFICATION_TIMEOUT):
                    for query in queries:
                        for item in await provider.search(client, query, api_key.strip()):
                            if item["url"] not in seen_urls:
                                seen_urls.add(item["url"])
                                candidates.append(item)
                            if len(candidates) >= MAX_SEARCH_RESULTS:
                                break
                        if len(candidates) >= MAX_SEARCH_RESULTS:
                            break
                    pages: list[dict[str, Any]] = []
                    for candidate in candidates:
                        page = await fetcher(client, candidate["url"])
                        if not page:
                            continue
                        pages.append(page)
                        if len(pages) == MAX_SOURCES:
                            break
            except (TimeoutError, httpx.HTTPError, VerificationError):
                offline["notice"] = "公开来源检索暂时不可用，已保留离线核验线索。"
                return offline
        result = assess_claims(offline, pages)
        _cache[cache_key] = (time.monotonic() + CACHE_SECONDS, copy.deepcopy(result))
        return result
    finally:
        if owns_client:
            await client.aclose()


async def verify_search_connection(provider_name: str, api_key: str) -> dict[str, Any]:
    """Run the smallest possible provider request without persisting the supplied key."""
    provider = get_search_provider(provider_name)
    async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT) as client:
        await provider.search(client, "NewsBrief 公开来源检索", api_key)
    return {
        "available": True,
        "provider": provider.name,
        "message": "公开来源检索服务连接成功。",
    }
