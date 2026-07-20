"""Local-only benchmark runner for the NewsBrief competition evaluation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np

from .facts import extract_facts, fact_quality
from .summarizer import (
    _centrality_scores,
    analyze_news,
    build_result_from_indices,
    build_summary,
)

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "benchmark_private"
ARTICLES_PATH = PRIVATE_DIR / "articles.json"
ANNOTATIONS_PATH = PRIVATE_DIR / "annotations.json"

CATEGORIES = [
    {"name": "校园", "count": 10},
    {"name": "民生", "count": 10},
    {"name": "政策", "count": 10},
    {"name": "科技", "count": 10},
    {"name": "财经", "count": 10},
    {"name": "文化", "count": 10},
]
METHODS = [
    ("lead3", "Lead-3 导语基线", "选择正文最靠前的三个有效句。"),
    ("textrank", "基础 TextRank", "仅基于句子相似度图与 PageRank 进行抽取。"),
    ("v14", "V1.4 新闻感知", "融合标题、导语与数字事实，但不加入事实覆盖奖励。"),
    ("v20", "V2.0 事实覆盖", "在新闻感知排序与 MMR 中奖励未覆盖的可核验事实。"),
]


class BenchmarkUnavailable(RuntimeError):
    """Raised when private source snapshots and annotations are not present locally."""


def _empty_methods() -> list[dict[str, Any]]:
    return [
        {
            "id": method_id,
            "name": name,
            "description": description,
            "status": "pending",
            "metrics": [],
        }
        for method_id, name, description in METHODS
    ]


def benchmark_overview() -> dict[str, Any]:
    available = ARTICLES_PATH.exists() and ANNOTATIONS_PATH.exists()
    return {
        "version": "2.2.0",
        "dataset": {
            "total": 60,
            "categories": CATEGORIES,
            "public_metadata_only": True,
            "private_dataset_available": available,
        },
        "methodology": [
            "自动评测比较 Lead-3、基础 TextRank、V1.4 新闻感知与 V2.0 事实覆盖四种方法。",
            "关键句 F1、事实覆盖率、数字事实召回率、重复风险、压缩率及 P95 耗时均在本机计算。",
            "人工盲评使用 24 篇均衡样本，匿名评估事实忠实度、信息覆盖、阅读清晰度与可信感。",
            "公开仓库仅保存来源元数据、标注规范与汇总结果，不发布新闻正文快照。",
        ],
        "methods": _empty_methods(),
        "human_review": {
            "reviewers_target": "5–8 名评测者；每个新闻-系统组合至少 3 份评分",
            "samples": 24,
            "dimensions": ["事实忠实度", "关键信息覆盖度", "阅读清晰度", "可信感"],
        },
    }


def _mmr_select(scores: np.ndarray, similarity: np.ndarray, target: int) -> list[int]:
    selected: list[int] = []
    available = set(range(len(scores)))
    while available and len(selected) < target:
        best_index = max(
            available,
            key=lambda index: 0.78 * float(scores[index])
            - 0.22 * max((float(similarity[index, chosen]) for chosen in selected), default=0.0),
        )
        selected.append(best_index)
        available.remove(best_index)
    return selected


def _f1(selected: set[int], expected: set[int]) -> float:
    if not selected or not expected:
        return 0.0
    overlap = len(selected & expected)
    precision = overlap / len(selected)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _number_recall(result: dict[str, Any], expected: list[str]) -> float:
    if not expected:
        return 1.0
    selected = set(result["selected_sentence_ids"])
    selected_text = "\n".join(
        sentence["text"] for sentence in result["source_sentences"] if sentence["id"] in selected
    )
    return sum(value in selected_text for value in expected) / len(expected)


def _metric(label: str, value: float, suffix: str = "%") -> dict[str, str]:
    return {"label": label, "value": f"{value:.1f}{suffix}", "detail": "基于本机私有标注集计算"}


def run_private_benchmark() -> dict[str, Any]:
    if not ARTICLES_PATH.exists() or not ANNOTATIONS_PATH.exists():
        raise BenchmarkUnavailable("未找到本机私有新闻快照与标注，无法运行评测。")
    articles = json.loads(ARTICLES_PATH.read_text(encoding="utf-8"))
    annotations = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
    if len(articles) < 60:
        raise BenchmarkUnavailable("私有评测集不足 60 篇，暂不生成竞赛结论。")

    records: dict[str, list[dict[str, float]]] = {method_id: [] for method_id, _, _ in METHODS}
    for article in articles:
        annotation = annotations.get(article["id"])
        if not annotation:
            continue
        analysis = analyze_news(article.get("title", ""), article["content"])
        target = min(3, len(analysis.candidates))
        expected_ids = set(annotation.get("key_sentence_ids", []))
        expected_numbers = annotation.get("number_values", [])
        method_results: dict[str, dict[str, Any]] = {}
        facts = extract_facts(analysis)

        started = time.perf_counter()
        lead_indices = list(range(target))
        method_results["lead3"] = build_result_from_indices(analysis, lead_indices, facts=facts)
        lead_elapsed = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        text_rank_indices = _mmr_select(
            _centrality_scores(analysis.candidates, analysis.similarity),
            analysis.similarity,
            target,
        )
        method_results["textrank"] = build_result_from_indices(
            analysis, text_rank_indices, facts=facts
        )
        text_rank_elapsed = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        v14_indices = _mmr_select(analysis.scores, analysis.similarity, target)
        method_results["v14"] = build_result_from_indices(analysis, v14_indices, facts=facts)
        v14_elapsed = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        method_results["v20"] = build_summary(analysis, "standard")
        v20_elapsed = (time.perf_counter() - started) * 1000

        elapsed = {
            "lead3": lead_elapsed,
            "textrank": text_rank_elapsed,
            "v14": v14_elapsed,
            "v20": v20_elapsed,
        }
        for method_id, result in method_results.items():
            selected = set(result["selected_sentence_ids"])
            quality = fact_quality(result["facts"], list(selected))
            records[method_id].append(
                {
                    "key_sentence_f1": _f1(selected, expected_ids) * 100,
                    "fact_coverage": quality["fact_coverage"],
                    "number_recall": _number_recall(result, expected_numbers) * 100,
                    "redundancy_risk": float(result["quality"]["redundancy_risk"] or 0),
                    "compression_ratio": float(result["metrics"]["compression_ratio"]),
                    "elapsed_ms": elapsed[method_id],
                }
            )

    overview = benchmark_overview()
    if not any(records.values()):
        raise BenchmarkUnavailable("私有标注集没有可计算的有效样本。")
    methods = []
    for method_id, name, description in METHODS:
        values = records[method_id]
        latencies = sorted(item["elapsed_ms"] for item in values)
        p95_index = min(len(latencies) - 1, max(0, round(len(latencies) * 0.95) - 1))
        methods.append(
            {
                "id": method_id,
                "name": name,
                "description": description,
                "status": "ready",
                "metrics": [
                    _metric("关键句 F1", mean(item["key_sentence_f1"] for item in values)),
                    _metric("事实覆盖率", mean(item["fact_coverage"] for item in values)),
                    _metric("数字事实召回", mean(item["number_recall"] for item in values)),
                    _metric("重复风险", mean(item["redundancy_risk"] for item in values)),
                    _metric("平均压缩率", mean(item["compression_ratio"] for item in values)),
                    _metric("P95 耗时", latencies[p95_index], " ms"),
                ],
            }
        )
    overview["methods"] = methods
    return overview
