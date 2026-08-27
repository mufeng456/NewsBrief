import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import benchmarks, main, verification
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
        assert capabilities["verification_engine"]["provider"] == "bocha"
        assert capabilities["verification_engine"]["provider_label"] == "博查 Web Search（国内默认）"
        assert capabilities["limits"]["max_characters"] == 8000


def test_article_import_extracts_a_bounded_editable_article_without_writing_history(monkeypatch):
    long_content = "第一段新闻正文。" * 1200

    async def fake_fetcher(_client, _url):
        return {
            "title": "导入测试新闻",
            "url": "https://news.example.com/article/1",
            "domain": "news.example.com",
            "tier": "other",
            "text": f"{long_content}\n第二段新闻正文。" * 3,
            "content_sha256": "a" * 64,
        }

    monkeypatch.setattr(main, "fetch_public_page", fake_fetcher)
    with TestClient(app) as client:
        before = client.get("/api/history").json()
        response = client.post("/api/articles/import", json={"url": "https://example.com/news"})
        after = client.get("/api/history").json()

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "导入测试新闻"
    assert payload["source_url"] == "https://news.example.com/article/1"
    assert payload["source_domain"] == "news.example.com"
    assert len(payload["content"].replace("\n", "")) <= 8000
    assert before == after == []


def test_extract_page_reads_cctv_style_embedded_article_content():
    html = """
    <html><head><title>央视测试新闻</title></head><body>
    <div id="content_area">正在加载</div>
    <script>
      var contentdate = '<p>[!--begin:htmlVideoCode--]video-id,0[!--end:htmlVideoCode--]</p><p>央视网消息：这是第一段具有足够长度的新闻正文，用于验证静态页面中的内嵌内容提取。</p><p>这是第二段具有足够长度的新闻正文，确保提取结果不会依赖浏览器执行页面脚本。</p>'
    </script>
    </body></html>
    """

    title, text = verification._extract_page(html, "fallback.example.com")

    assert title == "央视测试新闻"
    assert "第一段具有足够长度" in text
    assert "第二段具有足够长度" in text
    assert "htmlVideoCode" not in text


def test_extract_page_reads_standard_jsonld_article_body_and_metadata_title():
    html = """
    <html><head>
      <meta property="og:title" content="JSON-LD 新闻标题">
      <title>站点标题 - 不应优先</title>
      <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "JSON-LD 内嵌标题",
          "articleBody": "第一段 JSON-LD 正文包含完整新闻背景和事件进展，用于验证结构化数据提取。\n第二段 JSON-LD 正文补充了公开信息和后续安排，确保内容可用于摘要。"
        }
      </script>
    </head><body><p>页面侧栏中的无关短文案。</p></body></html>
    """

    title, text = verification._extract_page(html, "fallback.example.com")

    assert title == "JSON-LD 新闻标题"
    assert "第一段 JSON-LD 正文" in text
    assert "第二段 JSON-LD 正文" in text
    assert "侧栏中的无关" not in text


def test_extract_page_prefers_semantic_article_container_over_page_chrome():
    html = """
    <html><head><title>容器新闻标题</title></head><body>
      <aside><p>推荐阅读：这是侧栏推荐内容，不应被当作新闻正文导入工作台。</p></aside>
      <div class="article-content">
        <p>正文第一段说明了本次新闻事件的起因、参与主体和已经公开的处理安排。</p>
        <p>正文第二段补充了相关数据、时间节点以及后续将持续公布的进展信息。</p>
      </div>
      <footer><p>网站页脚版权与服务说明，不能进入用户导入的新闻正文。</p></footer>
    </body></html>
    """

    title, text = verification._extract_page(html, "fallback.example.com")

    assert title == "容器新闻标题"
    assert "正文第一段说明" in text
    assert "正文第二段补充" in text
    assert "侧栏推荐内容" not in text
    assert "网站页脚版权" not in text


def test_extract_page_reads_article_body_from_framework_json_and_gb18030_page():
    json_html = """
    <html><head><title>框架数据标题</title></head><body>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"article":{"type":"NewsArticle","headline":"框架内嵌标题","articleBody":"框架数据第一段新闻正文具备足够长度，可从应用状态中安全读取。\\n框架数据第二段新闻正文说明事件进展，不需要执行页面脚本。"}}}}
      </script>
    </body></html>
    """
    encoded_html = """
    <html><head><meta charset="gbk"><title>编码新闻标题</title></head><body>
      <div id="articleContent"><p>编码页面第一段新闻正文用于验证 GB18030 兼容解码和中文内容提取。</p>
      <p>编码页面第二段新闻正文补充了足够的信息，确保正文能够正常回填。</p></div>
    </body></html>
    """.encode("gb18030")

    framework_title, framework_text = verification._extract_page(json_html, "fallback.example.com")
    decoded_html = verification._decode_page_html(encoded_html, "text/html; charset=gbk")
    encoded_title, encoded_text = verification._extract_page(decoded_html, "fallback.example.com")

    assert framework_title == "框架内嵌标题"
    assert "框架数据第一段新闻正文" in framework_text
    assert "框架数据第二段新闻正文" in framework_text
    assert encoded_title == "编码新闻标题"
    assert "GB18030 兼容解码" in encoded_text


def test_article_import_rejects_invalid_or_unextractable_links(monkeypatch):
    async def unavailable_fetcher(_client, _url):
        return None

    monkeypatch.setattr(main, "fetch_public_page", unavailable_fetcher)
    with TestClient(app) as client:
        invalid = client.post("/api/articles/import", json={"url": "http://example.com/news"})
        unavailable = client.post("/api/articles/import", json={"url": "https://example.com/news"})

    assert invalid.status_code == 422
    assert unavailable.status_code == 422
    assert "手动粘贴" in unavailable.json()["detail"]


def test_article_import_explains_known_dynamic_msn_pages(monkeypatch):
    async def unavailable_fetcher(_client, _url):
        return None

    monkeypatch.setattr(main, "fetch_public_page", unavailable_fetcher)
    with TestClient(app) as client:
        response = client.post(
            "/api/articles/import",
            json={"url": "https://www.msn.cn/zh-cn/news/other/example/ar-AA29aMVW"},
        )

    assert response.status_code == 422
    assert "MSN" in response.json()["detail"]
    assert "动态加载" in response.json()["detail"]
    assert "原始发布媒体" in response.json()["detail"]


@pytest.mark.parametrize(
    ("url", "resource_label"),
    [
        ("https://cdn.example.com/news/photo.JPG?size=large", "图片"),
        ("https://cdn.example.com/news/report.mp4", "视频"),
    ],
)
def test_article_import_rejects_direct_media_resources(monkeypatch, url, resource_label):
    async def should_not_fetch(*_args, **_kwargs):
        raise AssertionError("直接媒体资源不应进入网页抓取流程")

    monkeypatch.setattr(main, "fetch_public_page", should_not_fetch)
    with TestClient(app) as client:
        response = client.post("/api/articles/import", json={"url": url})

    assert response.status_code == 422
    assert resource_label in response.json()["detail"]
    assert "新闻报道网页链接" in response.json()["detail"]


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
                "source_url": "https://news.example.com/article/7",
                "source_domain": "news.example.com",
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
    async def available(provider, _):
        assert provider == "bocha"
        return {
            "available": True,
            "provider": "bocha",
            "message": "公开来源检索服务连接成功。",
        }

    monkeypatch.setattr(main, "verify_search_connection", available)
    secret = "search-key-that-is-never-returned"
    with TestClient(app) as client:
        response = client.post(
            "/api/search-config/verify", json={"provider": "bocha", "api_key": secret}
        )
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["provider"] == "bocha"
    assert secret not in response.text


def test_search_configuration_saves_selected_provider_without_returning_the_key(monkeypatch):
    config_path = Path(__file__).with_name(".search-config-test.env")
    config_path.unlink(missing_ok=True)
    monkeypatch.setattr(main, "AI_ENV_PATH", config_path)
    monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
    monkeypatch.setenv("SEARCH_API_KEY", "")
    secret = "brave-key-that-is-never-returned"
    try:
        with TestClient(app) as client:
            response = client.put(
                "/api/search-config", json={"provider": "brave", "api_key": secret}
            )
        assert response.status_code == 200
        capability = response.json()["verification_engine"]
        assert capability["enabled"] is True
        assert capability["provider"] == "brave"
        assert capability["provider_label"] == "Brave Search（国际来源）"
        assert secret not in response.text
        saved = config_path.read_text(encoding="utf-8")
        assert "SEARCH_PROVIDER=brave" in saved
        assert f"SEARCH_API_KEY={secret}" in saved
    finally:
        config_path.unlink(missing_ok=True)


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


def test_history_backup_import_restores_records_and_skips_duplicates():
    sample = SAMPLES[3]
    with TestClient(app) as client:
        summary = client.post(
            "/api/summaries",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "length": "standard",
                "engine": "local",
            },
        ).json()
        saved = client.post(
            "/api/history",
            json={
                "title": sample["title"],
                "content": sample["content"],
                "source_url": "https://news.example.com/article/7",
                "source_domain": "news.example.com",
                "length": "standard",
                "result": summary,
            },
        )
        record_id = saved.json()["id"]
        client.patch(f"/api/history/{record_id}", json={"favorite": True, "rating": 4})

        backup = client.get("/api/history/backup")
        assert backup.status_code == 200
        assert backup.json()["format_version"] == 1
        assert len(backup.json()["records"]) == 1
        assert "api_key" not in backup.text.lower()

        client.delete("/api/history")
        imported = client.post("/api/history/import", json=backup.json())
        assert imported.status_code == 200, imported.json()
        assert imported.json() == {"imported": 1, "skipped": 0}

        restored = client.get("/api/history").json()
        assert len(restored) == 1
        assert restored[0]["favorite"] is True
        assert restored[0]["rating"] == 4
        assert restored[0]["facts"] == summary["facts"]
        assert restored[0]["source_url"] == "https://news.example.com/article/7"
        assert restored[0]["source_domain"] == "news.example.com"

        duplicate = client.post("/api/history/import", json=backup.json())
        assert duplicate.status_code == 200
        assert duplicate.json() == {"imported": 0, "skipped": 1}

        invalid = client.post("/api/history/import", json={"format_version": 1, "records": [{}]})
        assert invalid.status_code == 422
