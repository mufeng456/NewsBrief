import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import benchmarks, main
from backend.main import app
from backend.samples import SAMPLES


def online_verification_payload(client: TestClient):
    sample = SAMPLES[0]
    result = client.post(
        "/api/summaries",
        json={
            "title": sample["title"],
            "content": sample["content"],
            "length": "standard",
            "engine": "local",
        },
    ).json()
    verification = result["verification"]
    source = {
        "id": "source-1",
        "title": "官方公告",
        "url": "https://news.gov.cn/example",
        "domain": "news.gov.cn",
        "tier": "official",
        "excerpt": "官方来源摘录，用于支持当前新闻主张的主体、事件和时间信息。",
        "retrieved_at": "2026-07-20T00:00:00Z",
        "content_sha256": hashlib.sha256(b"official-source").hexdigest(),
    }
    verification.update(
        {
            "mode": "online",
            "status": "completed",
            "sources": [source],
            "searched_at": "2026-07-20T00:00:00Z",
        }
    )
    for claim in verification["claims"]:
        claim["status"] = "supported"
        claim["evidence_source_ids"] = [source["id"]]
    return sample, result, verification


def ai_review_payload(verification):
    reason = (
        "该建议仅基于当前来源短摘录与主张文本的对应关系。官方来源覆盖了主体、事件及现有的关键限定信息，"
        "但仍应打开来源原文核对完整语境、发布时间和后续更新，不能将该建议视为新闻真实性结论。"
    )
    return {
        "model": "test-evidence-model",
        "reviewed_at": "2026-07-20T00:00:00Z",
        "notice": "AI 建议仅辅助阅读，不构成新闻真实性结论。",
        "claims": [
            {
                "claim_id": claim["id"],
                "suggested_status": "supported",
                "reason": reason,
                "evidence_source_ids": ["source-1"],
            }
            for claim in verification["claims"]
        ],
    }


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_sessions = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(main, "SessionLocal", test_sessions)
    yield
    test_engine.dispose()


def test_health_and_capabilities():
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        capabilities = client.get("/api/capabilities").json()
        assert capabilities["local_engine"]["enabled"] is True
        assert capabilities["verification_engine"]["provider"] == "brave"
        assert capabilities["limits"]["max_characters"] == 8000


def test_application_factory_accepts_an_isolated_database_url():
    isolated_app = main.create_app("sqlite://", serve_frontend=False)
    with TestClient(isolated_app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_benchmark_overview_is_public_but_private_run_is_guarded():
    with TestClient(app) as client:
        overview = client.get("/api/benchmarks/overview")
        run = client.post("/api/benchmarks/run")
    assert overview.status_code == 200
    payload = overview.json()
    assert payload["dataset"]["total"] == 60
    assert len(payload["methods"]) == 4
    assert payload["dataset"]["public_metadata_only"] is True
    assert run.status_code == 409
    assert "私有" in run.json()["detail"]


def test_private_benchmark_uses_complete_results_for_each_baseline(monkeypatch):
    article = {"id": "fixture-0", "title": SAMPLES[0]["title"], "content": SAMPLES[0]["content"]}
    articles = [{**article, "id": f"fixture-{index}"} for index in range(60)]
    annotations = {
        f"fixture-{index}": {"key_sentence_ids": [0, 1], "number_values": []} for index in range(60)
    }

    class JsonPath:
        def __init__(self, payload):
            self.payload = payload

        def exists(self):
            return True

        def read_text(self, encoding):
            return json.dumps(self.payload)

    monkeypatch.setattr(benchmarks, "ARTICLES_PATH", JsonPath(articles))
    monkeypatch.setattr(benchmarks, "ANNOTATIONS_PATH", JsonPath(annotations))

    overview = benchmarks.run_private_benchmark()

    assert all(method["status"] == "ready" for method in overview["methods"])
    assert all(len(method["metrics"]) == 6 for method in overview["methods"])


def test_local_summary_is_source_traceable():
    sample = SAMPLES[0]
    with TestClient(app) as client:
        response = client.post(
            "/api/summaries",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "engine": "local",
            },
        )
    assert response.status_code == 200
    result = response.json()
    source_by_id = {item["id"]: item["text"] for item in result["source_sentences"]}
    assert result["engine"] == "local"
    assert len(result["bullets"]) >= 2
    assert result["quality"]["evidence_coverage"] == 100
    assert 0 <= result["quality"]["redundancy_risk"] <= 100
    assert result["quality"]["selected_sentence_count"] == len(result["selected_sentence_ids"])
    assert result["verification"]["mode"] == "offline"
    assert result["verification"]["claims"]
    for bullet in result["bullets"]:
        assert bullet["text"] in source_by_id.values()
        assert bullet["source_sentence_ids"]


def test_local_comparison_returns_three_lengths_without_creating_history():
    sample = SAMPLES[0]
    with TestClient(app) as client:
        before = client.get("/api/history").json()
        response = client.post(
            "/api/summaries/compare", json={"title": sample["title"], "content": sample["content"]}
        )
        after = client.get("/api/history").json()
    assert response.status_code == 200
    payload = response.json()
    assert [item["length"] for item in payload["results"]] == ["brief", "standard", "detailed"]
    assert [len(item["result"]["bullets"]) for item in payload["results"]] == [2, 4, 6]
    assert all(item["result"]["engine"] == "local" for item in payload["results"])
    assert before == after


def test_verification_endpoint_safely_returns_offline_preview_without_search_key(monkeypatch):
    sample = SAMPLES[0]
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/verifications", json={"title": sample["title"], "content": sample["content"]}
        )
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "offline"
    assert result["status"] == "unavailable"
    assert all(item["status"] == "offline_only" for item in result["claims"])


def test_ai_evidence_review_is_source_cited_and_persists_with_history(monkeypatch):
    secret = "ai-key-that-is-never-returned"
    monkeypatch.setenv("AI_API_KEY", secret)

    def review(verification, **kwargs):
        assert kwargs["api_key"] == secret
        return ai_review_payload(verification)

    monkeypatch.setattr(main, "review_evidence_with_ai", review)
    with TestClient(app) as client:
        sample, result, verification = online_verification_payload(client)
        response = client.post("/api/verifications/ai-review", json={"verification": verification})
        assert response.status_code == 200
        reviewed = response.json()
        assert reviewed["ai_review"]["claims"]
        assert secret not in response.text

        result["verification"] = reviewed
        saved = client.post(
            "/api/history",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "result": result,
            },
        )
        assert saved.status_code == 201
        assert saved.json()["verification"]["ai_review"] == reviewed["ai_review"]


def test_ai_evidence_review_requires_ai_and_valid_high_trust_citations(monkeypatch):
    with TestClient(app) as client:
        _, _, verification = online_verification_payload(client)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        unavailable = client.post("/api/verifications/ai-review", json={"verification": verification})
        assert unavailable.status_code == 409

        monkeypatch.setenv("AI_API_KEY", "ai-key-for-test")
        invalid = ai_review_payload(verification)
        invalid["claims"][0]["evidence_source_ids"] = ["missing-source"]
        monkeypatch.setattr(main, "review_evidence_with_ai", lambda *_args, **_kwargs: invalid)
        rejected = client.post("/api/verifications/ai-review", json={"verification": verification})

    assert rejected.status_code == 422


def test_local_comparison_applies_same_evidence_constraints_to_each_length():
    sample = SAMPLES[0]
    with TestClient(app) as client:
        response = client.post(
            "/api/summaries/compare",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "selection_constraints": {
                    "pinned_sentence_ids": [0],
                    "excluded_sentence_ids": [1],
                },
            },
        )
    assert response.status_code == 200
    for item in response.json()["results"]:
        result = item["result"]
        assert 0 in result["selected_sentence_ids"]
        assert 1 not in result["selected_sentence_ids"]
        assert result["selection_constraints"]["pinned_sentence_ids"] == [0]


def test_local_comparison_uses_existing_validation():
    with TestClient(app) as client:
        response = client.post("/api/summaries/compare", json={"content": "太短了。"})
    assert response.status_code == 422
    assert "80" in response.json()["detail"]


def test_summary_constraints_and_facts_round_trip_through_history():
    sample = SAMPLES[0]
    payload = {
        "title": sample["title"],
        "content": sample["content"],
        "length": "standard",
        "engine": "local",
        "selection_constraints": {"pinned_sentence_ids": [0], "excluded_sentence_ids": []},
    }
    with TestClient(app) as client:
        summary = client.post("/api/summaries", json=payload)
        assert summary.status_code == 200
        result = summary.json()
        assert result["facts"]
        assert 0 in result["selected_sentence_ids"]
        saved = client.post(
            "/api/history",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "result": result,
            },
        )
        assert saved.status_code == 201
        assert saved.json()["facts"] == result["facts"]
        assert saved.json()["selection_constraints"] == result["selection_constraints"]
        assert saved.json()["verification"] == {
            **result["verification"],
            "ai_review": None,
        }

        updated = client.patch(
            f"/api/history/{saved.json()['id']}", json={"verification": result["verification"]}
        )
        assert updated.status_code == 200
        assert updated.json()["verification"] == {
            **result["verification"],
            "ai_review": None,
        }


def test_legacy_history_without_verification_gets_offline_preview():
    sample = SAMPLES[0]
    with TestClient(app) as client:
        result = client.post(
            "/api/summaries",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "engine": "local",
            },
        ).json()
        result.pop("verification")
        saved = client.post(
            "/api/history",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "result": result,
            },
        )

    assert saved.status_code == 201
    preview = saved.json()["verification"]
    assert preview["mode"] == "offline"
    assert preview["status"] == "unavailable"
    assert preview["claims"]
    assert all(claim["status"] == "offline_only" for claim in preview["claims"])


def test_invalid_text_returns_friendly_validation_error():
    with TestClient(app) as client:
        response = client.post(
            "/api/summaries", json={"content": "太短了。", "length": "standard", "engine": "local"}
        )
    assert response.status_code == 422
    assert "80" in response.json()["detail"]


def test_history_rejects_incomplete_summary_result():
    with TestClient(app) as client:
        response = client.post(
            "/api/history",
            json={
                "title": "测试新闻",
                "content": "这是一段满足最小长度要求的测试新闻正文，用于确认历史接口不会接受不完整的摘要结果。"
                * 2,
                "length": "brief",
                "result": {},
            },
        )
    assert response.status_code == 422


def test_ai_request_falls_back_to_local_when_not_configured():
    sample = SAMPLES[2]
    with TestClient(app) as client:
        response = client.post(
            "/api/summaries",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "brief",
                "engine": "ai",
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["engine"] == "local"
    assert result["fallback_reason"]


def test_ai_configuration_enables_capability_without_returning_key(monkeypatch):
    config_path = Path(__file__).with_name(".ai-config-test.env")
    config_path.unlink(missing_ok=True)
    monkeypatch.setattr(main, "AI_ENV_PATH", config_path)
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("AI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AI_MODEL", "deepseek-chat")
    try:
        with TestClient(app) as client:
            response = client.put(
                "/api/ai-config",
                json={
                    "api_key": "test-key-that-is-never-returned",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                },
            )
        assert response.status_code == 200
        capability = response.json()["ai_engine"]
        assert capability["enabled"] is True
        assert capability["base_url"] == "https://api.deepseek.com"
        assert "test-key-that-is-never-returned" not in response.text
        assert "AI_API_KEY=test-key-that-is-never-returned" in config_path.read_text(
            encoding="utf-8"
        )
    finally:
        config_path.unlink(missing_ok=True)


def test_ai_configuration_rejects_blank_key_after_trimming():
    with TestClient(app) as client:
        response = client.post(
            "/api/ai-config/verify",
            json={
                "api_key": "          ",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            },
        )
    assert response.status_code == 422


def test_search_configuration_rejects_blank_key_after_trimming():
    with TestClient(app) as client:
        response = client.post("/api/search-config/verify", json={"api_key": "          "})
    assert response.status_code == 422


def test_search_connection_verification_never_returns_the_key(monkeypatch):
    async def available(_):
        return {
            "available": True,
            "provider": "brave",
            "message": "公开来源检索服务连接成功。",
        }

    monkeypatch.setattr(main, "verify_brave_connection", available)
    secret = "search-key-that-is-never-returned"
    with TestClient(app) as client:
        response = client.post("/api/search-config/verify", json={"api_key": secret})
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert secret not in response.text


def test_ai_connection_verification_never_returns_the_key(monkeypatch):
    monkeypatch.setattr(
        main,
        "verify_ai_connection",
        lambda _: {
            "available": True,
            "model": "deepseek-chat",
            "message": "连接成功，模型可以响应。",
        },
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/ai-config/verify",
            json={
                "api_key": "test-key-that-is-never-returned",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            },
        )
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert "test-key-that-is-never-returned" not in response.text


def test_ai_connection_failure_returns_safe_message(monkeypatch):
    def unavailable(_):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(main, "verify_ai_connection", unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/api/ai-config/verify",
            json={
                "api_key": "test-key-that-is-never-returned",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            },
        )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "超时" in response.json()["message"]


def test_history_can_save_and_update():
    sample = SAMPLES[1]
    with TestClient(app) as client:
        summary = client.post(
            "/api/summaries",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "brief",
                "engine": "local",
            },
        ).json()
        saved = client.post(
            "/api/history",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "brief",
                "result": summary,
            },
        )
        assert saved.status_code == 201
        record_id = saved.json()["id"]
        updated = client.patch(f"/api/history/{record_id}", json={"favorite": True, "rating": 5})
        assert updated.status_code == 200
        assert updated.json()["favorite"] is True
        assert updated.json()["rating"] == 5
        sorted_history = client.get("/api/history?sort=rating").json()
        assert sorted_history[0]["id"] == record_id
        assert sorted_history[0]["quality"]["evidence_coverage"] == 100
        client.delete(f"/api/history/{record_id}")
