"""Source-backed news verification assistance.

The module reports evidence states for claims extracted from a supplied news
article. It deliberately does not decide whether a news item is true or false.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .facts import extract_facts
from .summarizer import analyze_news, split_sentences, tokenize

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_QUERIES = 3
MAX_SEARCH_RESULTS = 8
MAX_SOURCES = 6
MAX_REDIRECTS = 3
MAX_SOURCE_BYTES = 1_500_000
SOURCE_TIMEOUT = 6.0
VERIFICATION_TIMEOUT = 20.0
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


def _extract_page(html: str, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else fallback_title
    paragraphs = [item.get_text(" ", strip=True) for item in soup.find_all("p")]
    paragraphs = [item for item in paragraphs if len(item) >= 24]
    text = "\n".join(paragraphs[:80])
    if not text:
        text = soup.get_text(" ", strip=True)
    return title[:180], text[:18_000]


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
        raw_html = b"".join(chunks).decode("utf-8", errors="ignore")
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


class BraveSearchProvider:
    """Small adapter around Brave's documented JSON web-search endpoint."""

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
    provider: BraveSearchProvider | None = None,
    client: httpx.AsyncClient | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Search public sources and return evidence states with safe degradation."""
    offline = offline_verification_for_article(title, content)
    if not api_key.strip():
        return offline
    cache_key = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return copy.deepcopy(cached[1])
    queries = build_queries(title, offline["claims"])
    if not queries:
        offline["notice"] = "无法从当前新闻中生成可用于公开检索的短查询。"
        return offline

    provider = provider or BraveSearchProvider()
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


async def verify_brave_connection(api_key: str) -> dict[str, Any]:
    """Run the smallest possible request without persisting the supplied key."""
    provider = BraveSearchProvider()
    async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT) as client:
        await provider.search(client, "NewsBrief", api_key)
    return {
        "available": True,
        "provider": "brave",
        "message": "公开来源检索服务连接成功。",
    }
