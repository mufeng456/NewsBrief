"""A source-traceable, news-aware Chinese extractive summarizer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import jieba
import networkx as nx
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .facts import extract_facts, fact_kinds_by_sentence, fact_quality

SummaryLength = Literal["brief", "standard", "detailed"]

STOP_WORDS = {
    "的",
    "了",
    "和",
    "与",
    "及",
    "在",
    "对",
    "将",
    "为",
    "是",
    "有",
    "也",
    "并",
    "等",
    "还",
    "通过",
    "一个",
    "以及",
    "进行",
    "相关",
    "表示",
    "可以",
    "目前",
    "其中",
    "这一",
    "本次",
    "后续",
}
TARGET_SENTENCES = {"brief": 2, "standard": 4, "detailed": 6}
FACT_PATTERN = re.compile(
    r"(?:\d{1,4}(?:\.\d+)?(?:%|亿元|万元|元|万|亿|人|名|家|项|次|条|公里|小时|分钟|天|月|日|年|时)|\d{1,2}[:：]\d{2})"
)


class SummarizationError(ValueError):
    """Raised when input cannot be reliably summarized."""


@dataclass(frozen=True)
class CandidateSentence:
    source_id: int
    text: str
    normalized: str
    token_set: frozenset[str]


@dataclass(frozen=True)
class NewsAnalysis:
    title: str
    original_characters: int
    raw_sentences: list[str]
    candidates: list[CandidateSentence]
    matrix: object
    similarity: np.ndarray
    scores: np.ndarray
    feature_names: np.ndarray


@dataclass(frozen=True)
class SelectionConstraints:
    pinned_sentence_ids: tuple[int, ...] = ()
    excluded_sentence_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, list[int]]:
        return {
            "pinned_sentence_ids": list(self.pinned_sentence_ids),
            "excluded_sentence_ids": list(self.excluded_sentence_ids),
        }


def clean_text(content: str) -> str:
    content = content.replace("\u3000", " ").replace("\r\n", "\n")
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def split_sentences(content: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", content)
    return [part.strip(" \t\n，,；;。！？!?") for part in parts if part.strip()]


def tokenize(text: str) -> list[str]:
    return [
        word.strip()
        for word in jieba.lcut(text)
        if len(word.strip()) > 1 and word.strip() not in STOP_WORDS and not word.strip().isdigit()
    ]


def validate_content(content: str) -> tuple[str, list[str]]:
    cleaned = clean_text(content)
    if len(cleaned) < 80:
        raise SummarizationError("新闻正文至少需要 80 个有效字符，才能生成可靠摘要。")
    if len(cleaned) > 8000:
        raise SummarizationError("新闻正文超过 8,000 个字符，请分段处理后再试。")
    raw_sentences = split_sentences(cleaned)
    valid_sentences = [sentence for sentence in raw_sentences if len(sentence) >= 8]
    if len(valid_sentences) < 2:
        raise SummarizationError("有效新闻句不足，建议补充更完整的正文内容。")
    if len(valid_sentences) > 180:
        raise SummarizationError("有效句超过 180 句，请缩短内容后再试。")
    return cleaned, raw_sentences


def _build_candidates(raw_sentences: list[str]) -> list[CandidateSentence]:
    candidates: list[CandidateSentence] = []
    seen: set[str] = set()
    for source_id, sentence in enumerate(raw_sentences):
        compact = re.sub(r"\s+", "", sentence)
        if len(compact) < 8 or compact in seen:
            continue
        tokens = tokenize(sentence)
        if not tokens:
            continue
        seen.add(compact)
        candidates.append(
            CandidateSentence(source_id, sentence, " ".join(tokens), frozenset(tokens))
        )
    if len(candidates) < 2:
        raise SummarizationError("正文中的有效信息不足，无法构建句子关联。")
    return candidates


def _normalize(values: np.ndarray) -> np.ndarray:
    minimum = float(values.min())
    maximum = float(values.max())
    if maximum - minimum < 1e-9:
        return np.ones_like(values, dtype=float)
    return (values - minimum) / (maximum - minimum)


def _centrality_scores(candidates: list[CandidateSentence], similarity: np.ndarray) -> np.ndarray:
    graph = nx.Graph()
    graph.add_nodes_from(range(len(candidates)))
    for row in range(len(candidates)):
        for column in range(row + 1, len(candidates)):
            weight = float(similarity[row, column])
            if weight >= 0.06:
                graph.add_edge(row, column, weight=weight)
    if graph.number_of_edges() == 0:
        raw_scores = np.array(
            [1 / (candidate.source_id + 1) for candidate in candidates], dtype=float
        )
    else:
        ranks = nx.pagerank(graph, weight="weight")
        raw_scores = np.array(
            [ranks.get(index, 0.0) for index in range(len(candidates))], dtype=float
        )
    return _normalize(raw_scores)


def _title_alignment(title_terms: frozenset[str], candidate: CandidateSentence) -> float:
    if not title_terms:
        return 0.0
    return len(title_terms & candidate.token_set) / len(title_terms)


def _lead_position(candidate: CandidateSentence, sentence_count: int) -> float:
    if sentence_count <= 1:
        return 1.0
    return max(0.0, 1 - candidate.source_id / (sentence_count - 1))


def _fact_score(sentence: str) -> float:
    return min(len(FACT_PATTERN.findall(sentence)), 3) / 3


def _news_scores(
    title: str,
    raw_sentences: list[str],
    candidates: list[CandidateSentence],
    similarity: np.ndarray,
) -> np.ndarray:
    centrality = _centrality_scores(candidates, similarity)
    title_terms = frozenset(tokenize(title))
    centrality_weight = 0.70 if title_terms else 0.84
    title_scores = np.array(
        [_title_alignment(title_terms, candidate) for candidate in candidates], dtype=float
    )
    position_scores = np.array(
        [_lead_position(candidate, len(raw_sentences)) for candidate in candidates], dtype=float
    )
    fact_scores = np.array([_fact_score(candidate.text) for candidate in candidates], dtype=float)
    return (
        centrality_weight * centrality
        + 0.14 * title_scores
        + 0.10 * position_scores
        + 0.06 * fact_scores
    )


def analyze_news(title: str, content: str) -> NewsAnalysis:
    cleaned, raw_sentences = validate_content(content)
    candidates = _build_candidates(raw_sentences)
    vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
    try:
        matrix = vectorizer.fit_transform([candidate.normalized for candidate in candidates])
    except ValueError as error:
        raise SummarizationError("未能从正文中提取足够的中文关键词。") from error
    similarity = cosine_similarity(matrix)
    scores = _news_scores(title.strip(), raw_sentences, candidates, similarity)
    return NewsAnalysis(
        title=title.strip(),
        original_characters=len(cleaned),
        raw_sentences=raw_sentences,
        candidates=candidates,
        matrix=matrix,
        similarity=similarity,
        scores=scores,
        feature_names=vectorizer.get_feature_names_out(),
    )


def _validate_constraints(
    analysis: NewsAnalysis, constraints: dict[str, Any] | None
) -> SelectionConstraints:
    raw = constraints or {}
    try:
        pinned = tuple(int(item) for item in raw.get("pinned_sentence_ids", []))
        excluded = tuple(int(item) for item in raw.get("excluded_sentence_ids", []))
    except (TypeError, ValueError) as error:
        raise SummarizationError("证据约束中的句索引必须为整数。") from error
    if len(set(pinned)) != len(pinned) or len(set(excluded)) != len(excluded):
        raise SummarizationError("固定句和排除句中不能包含重复索引。")
    if set(pinned) & set(excluded):
        raise SummarizationError("同一句不能同时被固定和排除。")
    candidate_ids = {candidate.source_id for candidate in analysis.candidates}
    invalid = (set(pinned) | set(excluded)) - candidate_ids
    if invalid:
        raise SummarizationError("证据约束包含无法参与摘要的原文句。")
    return SelectionConstraints(tuple(sorted(pinned)), tuple(sorted(excluded)))


def _pick_indices(
    scores: np.ndarray,
    similarity: np.ndarray,
    target: int,
    pinned: list[int],
    excluded: set[int],
    source_ids: list[int],
    fact_kinds: dict[int, set[str]],
) -> list[int]:
    if len(pinned) > target:
        raise SummarizationError("固定句数量超过当前摘要长度，请选择更长的摘要档位。")
    selected: list[int] = list(pinned)
    available = set(range(len(scores))) - set(pinned) - excluded
    if not selected and not available:
        raise SummarizationError("排除句已覆盖全部可用信息，无法生成摘要。")
    all_fact_kinds = set().union(*fact_kinds.values()) if fact_kinds else set()
    while available and len(selected) < target:
        best_index = -1
        best_value = float("-inf")
        covered_kinds = (
            set().union(*(fact_kinds.get(source_ids[index], set()) for index in selected))
            if selected
            else set()
        )
        for index in available:
            redundancy = max((similarity[index, chosen] for chosen in selected), default=0.0)
            newly_covered = fact_kinds.get(source_ids[index], set()) - covered_kinds
            fact_gain = len(newly_covered) / len(all_fact_kinds) if all_fact_kinds else 0.0
            value = 0.72 * float(scores[index]) - 0.20 * float(redundancy) + 0.08 * fact_gain
            if value > best_value:
                best_value = value
                best_index = index
        if best_index < 0:
            break
        selected.append(best_index)
        available.remove(best_index)
    return selected


def _matrix_redundancy_risk(similarity: np.ndarray, selected: list[int]) -> int:
    if len(selected) < 2:
        return 0
    maximum = max(
        float(similarity[left, right])
        for offset, left in enumerate(selected)
        for right in selected[offset + 1 :]
    )
    return round(maximum * 100)


def calculate_redundancy_risk(texts: list[str]) -> int:
    """Return a transparent 0-100 lexical-overlap risk for displayed AI bullets."""
    normalized = [" ".join(tokenize(text)) for text in texts]
    normalized = [text for text in normalized if text]
    if len(normalized) < 2:
        return 0
    try:
        matrix = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b").fit_transform(normalized)
    except ValueError:
        return 0
    similarity = cosine_similarity(matrix)
    np.fill_diagonal(similarity, 0)
    return round(float(similarity.max()) * 100)


def build_result_from_indices(
    analysis: NewsAnalysis,
    selected_indices: list[int],
    *,
    facts: list[dict[str, Any]] | None = None,
    selection_constraints: SelectionConstraints | None = None,
) -> dict:
    """Build a complete traceable result from a method's candidate selections."""
    if not selected_indices:
        raise SummarizationError("No source sentences are available for the summary.")
    if len(set(selected_indices)) != len(selected_indices):
        raise SummarizationError("Summary candidates cannot repeat.")
    if any(index < 0 or index >= len(analysis.candidates) for index in selected_indices):
        raise SummarizationError("Summary candidate index is out of range.")

    selected = sorted(selected_indices, key=lambda index: analysis.candidates[index].source_id)
    chosen = [analysis.candidates[index] for index in selected]
    extracted_facts = facts if facts is not None else extract_facts(analysis)
    constraints = selection_constraints or SelectionConstraints()
    selected_matrix = analysis.matrix[selected]
    keyword_weights = np.asarray(selected_matrix.sum(axis=0)).ravel()
    keyword_order = keyword_weights.argsort()[::-1]
    keywords = [
        analysis.feature_names[index]
        for index in keyword_order
        if len(analysis.feature_names[index]) > 1
    ][:8]
    generated_title = analysis.title or (
        chosen[0].text[:26] + ("..." if len(chosen[0].text) > 26 else "")
    )
    summary_text = "。".join(item.text.rstrip("。！？!? ") for item in chosen) + "。"
    selected_ids = {item.source_id for item in chosen}
    score_lookup = {
        analysis.candidates[index].source_id: round(float(analysis.scores[index]), 4)
        for index in range(len(analysis.candidates))
    }
    source_sentences = [
        {
            "id": source_id,
            "text": sentence,
            "selected": source_id in selected_ids,
            "score": score_lookup.get(source_id, 0.0),
        }
        for source_id, sentence in enumerate(analysis.raw_sentences)
    ]
    return {
        "title": generated_title,
        "summary": summary_text,
        "bullets": [
            {"text": item.text, "source_sentence_ids": [item.source_id]} for item in chosen
        ],
        "keywords": keywords,
        "source_sentences": source_sentences,
        "selected_sentence_ids": [item.source_id for item in chosen],
        "metrics": {
            "original_characters": analysis.original_characters,
            "summary_characters": len(summary_text),
            "compression_ratio": round(len(summary_text) / analysis.original_characters * 100, 1),
            "sentence_count": len(analysis.raw_sentences),
        },
        "quality": {
            "evidence_coverage": 100,
            "redundancy_risk": _matrix_redundancy_risk(analysis.similarity, selected),
            "selected_sentence_count": len(chosen),
            "source_sentence_count": len(analysis.raw_sentences),
            **fact_quality(extracted_facts, [item.source_id for item in chosen]),
        },
        "facts": extracted_facts,
        "selection_constraints": constraints.as_dict(),
        "engine": "local",
        "engine_label": "\u672c\u5730\u53ef\u9760\u6458\u8981",
        "fallback_reason": None,
    }


def build_summary(
    analysis: NewsAnalysis,
    length: SummaryLength,
    selection_constraints: dict[str, Any] | None = None,
) -> dict:
    if length not in TARGET_SENTENCES:
        raise SummarizationError("不支持的摘要长度。")
    constraints = _validate_constraints(analysis, selection_constraints)
    source_ids = [candidate.source_id for candidate in analysis.candidates]
    source_to_index = {source_id: index for index, source_id in enumerate(source_ids)}
    pinned = [source_to_index[source_id] for source_id in constraints.pinned_sentence_ids]
    excluded = {source_to_index[source_id] for source_id in constraints.excluded_sentence_ids}
    available_count = len(analysis.candidates) - len(excluded)
    if available_count <= 0:
        raise SummarizationError("排除句已覆盖全部可用信息，无法生成摘要。")
    target = min(TARGET_SENTENCES[length], available_count)
    facts = extract_facts(analysis)
    selected = _pick_indices(
        analysis.scores,
        analysis.similarity,
        target,
        pinned,
        excluded,
        source_ids,
        fact_kinds_by_sentence(facts),
    )
    return build_result_from_indices(
        analysis,
        selected,
        facts=facts,
        selection_constraints=constraints,
    )


def summarize(
    title: str,
    content: str,
    length: SummaryLength = "standard",
    selection_constraints: dict[str, Any] | None = None,
) -> dict:
    return build_summary(analyze_news(title, content), length, selection_constraints)


def compare_summaries(
    title: str, content: str, selection_constraints: dict[str, Any] | None = None
) -> dict[SummaryLength, dict]:
    analysis = analyze_news(title, content)
    return {
        length: build_summary(analysis, length, selection_constraints)
        for length in TARGET_SENTENCES
    }
