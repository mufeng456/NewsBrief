import pytest

from backend.summarizer import (
    SummarizationError,
    analyze_news,
    build_result_from_indices,
    compare_summaries,
    summarize,
)

NEWS_TEXT = """
长沙市今日开通两条夜间公交线路，覆盖高校、医院和地铁换乘站，末班车延长至23时30分。
市交通部门表示，新线路将优先服务夜间学习、就医和加班人群，并根据客流变化调整班次。
本次试运行计划持续三个月，首周将安排志愿者在重点站点提供换乘指引。
为保障安全，运营企业已在车辆上增设夜间照明提示和紧急联系标识。
数据显示，两条线路预计每天服务超过2800名乘客，并减少约15%的夜间绕行时间。
试运行结束后，主管部门将公开乘客反馈并决定是否扩展至更多社区。
""".strip()


def test_title_and_fact_signals_keep_the_lead_sentence():
    result = summarize("长沙开通两条夜间公交线路", NEWS_TEXT, "brief")
    assert result["selected_sentence_ids"][0] == 0
    assert any("2800名乘客" in bullet["text"] for bullet in result["bullets"])


def test_summary_without_title_remains_source_traceable():
    result = summarize("", NEWS_TEXT, "standard")
    source_by_id = {item["id"] for item in result["source_sentences"]}
    assert result["title"]
    assert all(item in source_by_id for item in result["selected_sentence_ids"])
    assert result["quality"]["evidence_coverage"] == 100


def test_duplicate_sentences_are_not_selected_twice():
    content = NEWS_TEXT + "\n" + NEWS_TEXT.splitlines()[0]
    result = summarize("长沙开通两条夜间公交线路", content, "detailed")
    bullet_texts = [item["text"] for item in result["bullets"]]
    assert len(bullet_texts) == len(set(bullet_texts))


def test_result_builder_keeps_every_field_aligned_with_selected_sentences():
    analysis = analyze_news("长沙开通两条夜间公交线路", NEWS_TEXT)
    result = build_result_from_indices(analysis, [0, 4])

    assert result["selected_sentence_ids"] == [0, 4]
    assert [item["source_sentence_ids"] for item in result["bullets"]] == [[0], [4]]
    assert [item["id"] for item in result["source_sentences"] if item["selected"]] == [0, 4]
    assert result["quality"]["selected_sentence_count"] == 2
    assert "2800" in result["summary"]


def test_comparison_reuses_valid_source_mappings_for_all_lengths():
    variants = compare_summaries("长沙开通两条夜间公交线路", NEWS_TEXT)
    assert list(variants) == ["brief", "standard", "detailed"]
    assert [len(variants[length]["bullets"]) for length in variants] == [2, 4, 6]
    for result in variants.values():
        source_by_id = {item["id"] for item in result["source_sentences"]}
        assert all(item in source_by_id for item in result["selected_sentence_ids"])


def test_local_facts_are_source_backed_and_measured():
    result = summarize("长沙开通两条夜间公交线路", NEWS_TEXT, "standard")
    source_ids = {item["id"] for item in result["source_sentences"]}

    assert {"event", "number"}.issubset({item["kind"] for item in result["facts"]})
    assert all(set(item["evidence_sentence_ids"]).issubset(source_ids) for item in result["facts"])
    assert result["quality"]["facts_found"] == len(result["facts"])
    assert 0 <= result["quality"]["fact_coverage"] <= 100


def test_pinned_and_excluded_evidence_constraints_control_selection():
    result = summarize(
        "长沙开通两条夜间公交线路",
        NEWS_TEXT,
        "brief",
        {"pinned_sentence_ids": [4], "excluded_sentence_ids": [0]},
    )

    assert 4 in result["selected_sentence_ids"]
    assert 0 not in result["selected_sentence_ids"]
    assert result["selection_constraints"] == {
        "pinned_sentence_ids": [4],
        "excluded_sentence_ids": [0],
    }


def test_invalid_evidence_constraints_return_friendly_errors():
    with pytest.raises(SummarizationError, match="固定句数量"):
        summarize(
            "长沙开通两条夜间公交线路",
            NEWS_TEXT,
            "brief",
            {"pinned_sentence_ids": [0, 1, 2], "excluded_sentence_ids": []},
        )

    with pytest.raises(SummarizationError, match="同时被固定和排除"):
        summarize(
            "长沙开通两条夜间公交线路",
            NEWS_TEXT,
            "brief",
            {"pinned_sentence_ids": [0], "excluded_sentence_ids": [0]},
        )
