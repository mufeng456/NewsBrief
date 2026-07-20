import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from backend import evidence_review
from backend.samples import SAMPLES
from backend.summarizer import summarize
from backend.verification import (
    build_offline_verification,
    is_safe_public_url,
    run_online_verification,
)


class FakeProvider:
    async def search(self, _client, _query, _api_key):
        return [
            {
                "title": "官方公告",
                "url": "https://news.gov.cn/example",
                "description": "公开来源摘要",
            }
        ]


async def fake_fetcher(_client, _url):
    content = SAMPLES[0]["content"]
    return {
        "title": "官方公告",
        "url": "https://news.gov.cn/example",
        "domain": "news.gov.cn",
        "tier": "official",
        "text": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


async def private_resolver(_hostname):
    return ["127.0.0.1"]


async def public_resolver(_hostname):
    return ["8.8.8.8"]


def online_verification():
    return asyncio.run(
        run_online_verification(
            SAMPLES[0]["title"],
            SAMPLES[0]["content"],
            "brave-key-for-test",
            provider=FakeProvider(),
            fetcher=fake_fetcher,
        )
    )


def review_payload(verification, *, source_id="source-1"):
    reason = (
        "该建议仅比较当前提供的来源短摘录与主张文本。官方来源在主体、核心事件和已给定的限定信息上具有明确对应，"
        "但仍应打开原始报道核对完整语境、发布时间及后续更新，不能将建议视为新闻真实性结论。"
    )
    return {
        "claims": [
            {
                "claim_id": claim["id"],
                "suggested_status": "supported",
                "reason": reason,
                "evidence_source_ids": [source_id],
            }
            for claim in verification["claims"]
        ]
    }


class FakeOpenAI:
    payload = {}
    last_request = {}

    def __init__(self, **_kwargs):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        FakeOpenAI.last_request = kwargs
        return SimpleNamespace(
            model="test-evidence-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(FakeOpenAI.payload)))],
        )


def test_offline_claims_are_source_backed_and_not_truth_labels():
    sample = SAMPLES[0]
    summary = summarize(sample["title"], sample["content"])
    verification = build_offline_verification(summary["facts"], summary["source_sentences"])
    source_ids = {sentence["id"] for sentence in summary["source_sentences"]}

    assert verification["mode"] == "offline"
    assert verification["status"] == "unavailable"
    assert verification["claims"]
    assert verification["claims"][0]["kind"] == "event"
    assert all(claim["status"] == "offline_only" for claim in verification["claims"])
    assert all(set(claim["source_sentence_ids"]).issubset(source_ids) for claim in verification["claims"])


def test_online_verification_uses_source_evidence_without_truth_verdict():
    result = online_verification()

    assert result["mode"] == "online"
    assert result["sources"][0]["tier"] == "official"
    assert any(claim["status"] == "supported" for claim in result["claims"])
    assert all(claim["status"] in {"supported", "partial", "unverified", "conflicting"} for claim in result["claims"])


def test_public_url_guard_rejects_local_or_non_https_destinations():
    assert not asyncio.run(is_safe_public_url("http://example.com", public_resolver))
    assert not asyncio.run(is_safe_public_url("https://localhost/news", public_resolver))
    assert not asyncio.run(is_safe_public_url("https://example.com/news", private_resolver))
    assert asyncio.run(is_safe_public_url("https://example.com/news", public_resolver))


def test_ai_review_is_source_cited_and_does_not_receive_urls(monkeypatch):
    verification = online_verification()
    FakeOpenAI.payload = review_payload(verification)
    monkeypatch.setattr(evidence_review, "OpenAI", FakeOpenAI)

    review = evidence_review.review_evidence_with_ai(
        verification,
        api_key="ai-key-for-test",
        base_url="https://api.example.com",
        model="test-evidence-model",
    )

    assert review["model"] == "test-evidence-model"
    assert len(review["claims"]) == len(verification["claims"])
    assert all(item["evidence_source_ids"] == ["source-1"] for item in review["claims"])
    prompt = FakeOpenAI.last_request["messages"][1]["content"]
    assert '"url"' not in prompt
    assert '"excerpt"' in prompt


@pytest.mark.parametrize("source_id", ["missing-source", "source-1"])
def test_ai_review_rejects_invalid_or_low_trust_evidence(monkeypatch, source_id):
    verification = online_verification()
    if source_id == "source-1":
        verification["sources"][0]["tier"] = "other"
    FakeOpenAI.payload = review_payload(verification, source_id=source_id)
    monkeypatch.setattr(evidence_review, "OpenAI", FakeOpenAI)

    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.review_evidence_with_ai(
            verification,
            api_key="ai-key-for-test",
            base_url="https://api.example.com",
            model="test-evidence-model",
        )
