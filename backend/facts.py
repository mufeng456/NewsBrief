"""Rule-based factual extraction with sentence-level evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .summarizer import NewsAnalysis


FACT_LABELS = {
    "subject": "新闻主体",
    "time": "时间节点",
    "location": "发生地点",
    "event": "核心事件",
    "number": "关键数值",
    "impact": "影响与后续",
}
FACT_ORDER = ("subject", "time", "location", "event", "number", "impact")

TIME_PATTERN = re.compile(
    r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}[：:]\d{2}|"
    r"\d{1,2}时(?:\d{1,2}分)?|\d{1,2}(?:小时|分钟|天|周|个月|月|年)|"
    r"今日|近日|当天|今年|明年|本周|下周|未来)"
)
NUMBER_PATTERN = re.compile(
    r"\d{1,4}(?:\.\d+)?(?:%|亿元|万元|元|万|亿|人|名|家|项|次|条|公里|小时|分钟|天|月|年)"
)
LOCATION_PATTERN = re.compile(
    r"(?:[\u4e00-\u9fff]{2,8}?(?:省|市|自治区|区|县|镇|乡|街道|社区|园区)|"
    r"[\u4e00-\u9fff]{2,10}?(?:医院|图书馆|学校))"
)
SUBJECT_PATTERN = re.compile(
    r"(?:[\u4e00-\u9fffA-Za-z0-9]{2,16}?(?:大学图书馆|大学|学校|图书馆|医院|部门|委员会|政府|公司|集团|研究院|中心|局|厅|院))"
)
IMPACT_WEIGHTS = {
    "计划": 5,
    "决定": 5,
    "下一步": 5,
    "后续": 4,
    "扩展": 4,
    "预计": 3,
    "反馈": 3,
    "推动": 2,
    "改善": 2,
    "提升": 2,
    "将": 1,
}


def _candidate_order(analysis: NewsAnalysis) -> list[int]:
    ranked = sorted(
        range(len(analysis.candidates)),
        key=lambda index: (-float(analysis.scores[index]), analysis.candidates[index].source_id),
    )
    return [analysis.candidates[index].source_id for index in ranked]


def _matches_by_sentence(
    pattern: re.Pattern[str], sentences: list[str], candidate_ids: set[int]
) -> list[tuple[str, int]]:
    matches: list[tuple[str, int]] = []
    for source_id, sentence in enumerate(sentences):
        if source_id not in candidate_ids:
            continue
        for value in pattern.findall(sentence):
            cleaned = value.strip(" ，,。；;：:")
            if cleaned:
                matches.append((cleaned, source_id))
    return matches


def _first_ranked_match(
    matches: list[tuple[str, int]], rank: dict[int, int]
) -> tuple[str, int] | None:
    if not matches:
        return None
    return min(matches, key=lambda item: (rank.get(item[1], 10_000), item[1], item[0]))


def _group_values(
    matches: list[tuple[str, int]], rank: dict[int, int], limit: int = 3
) -> tuple[str, list[int]] | None:
    unique: dict[str, int] = {}
    for value, source_id in matches:
        current = unique.get(value)
        if current is None or rank.get(source_id, 10_000) < rank.get(current, 10_000):
            unique[value] = source_id
    ranked = sorted(unique.items(), key=lambda item: (rank.get(item[1], 10_000), item[1], item[0]))[
        :limit
    ]
    if not ranked:
        return None
    return "、".join(value for value, _ in ranked), sorted({source_id for _, source_id in ranked})


def _fact(kind: str, value: str, evidence_sentence_ids: list[int]) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": FACT_LABELS[kind],
        "value": value,
        "evidence_sentence_ids": evidence_sentence_ids,
        "method": "local_rule",
    }


def extract_facts(analysis: NewsAnalysis) -> list[dict[str, Any]]:
    """Extract only source-supported facts; absent fields are intentionally omitted."""
    candidate_ids = {candidate.source_id for candidate in analysis.candidates}
    ordered_ids = _candidate_order(analysis)
    rank = {source_id: index for index, source_id in enumerate(ordered_ids)}
    facts: dict[str, dict[str, Any]] = {}

    subject_matches = _matches_by_sentence(SUBJECT_PATTERN, analysis.raw_sentences, candidate_ids)
    subject = min(subject_matches, key=lambda item: (item[1], item[0])) if subject_matches else None
    if subject:
        facts["subject"] = _fact("subject", subject[0], [subject[1]])

    time = _group_values(
        _matches_by_sentence(TIME_PATTERN, analysis.raw_sentences, candidate_ids), rank
    )
    if time:
        facts["time"] = _fact("time", time[0], time[1])

    location = _first_ranked_match(
        _matches_by_sentence(LOCATION_PATTERN, analysis.raw_sentences, candidate_ids), rank
    )
    if location:
        facts["location"] = _fact("location", location[0], [location[1]])

    if analysis.candidates:
        event_id = min(candidate.source_id for candidate in analysis.candidates)
        facts["event"] = _fact("event", analysis.raw_sentences[event_id], [event_id])

    numbers = _group_values(
        _matches_by_sentence(NUMBER_PATTERN, analysis.raw_sentences, candidate_ids), rank
    )
    if numbers:
        facts["number"] = _fact("number", numbers[0], numbers[1])

    impact_candidates = [source_id for source_id in ordered_ids if source_id != event_id]
    if impact_candidates:
        impact_id = max(
            impact_candidates,
            key=lambda source_id: (
                sum(
                    weight
                    for term, weight in IMPACT_WEIGHTS.items()
                    if term in analysis.raw_sentences[source_id]
                ),
                -rank[source_id],
            ),
        )
        if any(term in analysis.raw_sentences[impact_id] for term in IMPACT_WEIGHTS):
            facts["impact"] = _fact("impact", analysis.raw_sentences[impact_id], [impact_id])

    return [facts[kind] for kind in FACT_ORDER if kind in facts]


def fact_kinds_by_sentence(facts: list[dict[str, Any]]) -> dict[int, set[str]]:
    mapping: dict[int, set[str]] = defaultdict(set)
    for fact in facts:
        for source_id in fact["evidence_sentence_ids"]:
            mapping[source_id].add(fact["kind"])
    return dict(mapping)


def fact_quality(facts: list[dict[str, Any]], selected_sentence_ids: list[int]) -> dict[str, int]:
    selected = set(selected_sentence_ids)
    covered = sum(bool(selected & set(fact["evidence_sentence_ids"])) for fact in facts)
    found = len(facts)
    return {
        "fact_coverage": round(covered / found * 100) if found else 0,
        "facts_found": found,
        "facts_covered": covered,
    }
