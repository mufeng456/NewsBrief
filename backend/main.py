"""FastAPI application for NewsBrief."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    case,
    create_engine,
    desc,
    inspect,
    or_,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .benchmarks import BenchmarkUnavailable, benchmark_overview, run_private_benchmark
from .evidence_review import AIEvidenceReview, EvidenceReviewError, review_evidence_with_ai
from .facts import fact_quality
from .samples import SAMPLES
from .summarizer import SummarizationError, calculate_redundancy_risk, compare_summaries, summarize
from .verification import (
    MAX_SOURCES,
    build_offline_verification,
    fetch_public_page,
    get_search_provider,
    offline_verification_for_article,
    run_online_verification,
    verify_search_connection,
)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = BASE_DIR.parent / "dist"
DATABASE_URL = f"sqlite:///{BASE_DIR / 'newsbrief.db'}"
AI_ENV_PATH = BASE_DIR / ".env"
AI_ENV_DEFAULTS = {
    "AI_BASE_URL": "https://api.deepseek.com",
    "AI_MODEL": "deepseek-chat",
}
SEARCH_ENV_DEFAULTS = {"SEARCH_PROVIDER": "bocha"}
SEARCH_PROVIDER_LABELS = {
    "bocha": "博查 Web Search（国内默认）",
    "brave": "Brave Search（国际来源）",
}
MAX_ARTICLE_CONTENT_CHARACTERS = 8000
MEDIA_RESOURCE_EXTENSIONS = {
    "image": {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".webp"},
    "video": {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".webm"},
}
KNOWN_DYNAMIC_ARTICLE_HOSTS = {"msn.com", "msn.cn"}
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def configure_database(database_url: str) -> None:
    """Point the application at an explicit SQLite database for tests or demos."""
    global engine, SessionLocal
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class HistoryRecord(Base):
    __tablename__ = "history_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(180))
    content: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    bullets_json: Mapped[str] = mapped_column(Text)
    keywords_json: Mapped[str] = mapped_column(Text)
    selected_ids_json: Mapped[str] = mapped_column(Text)
    source_sentences_json: Mapped[str] = mapped_column(Text)
    quality_json: Mapped[str] = mapped_column(Text, default="{}")
    facts_json: Mapped[str] = mapped_column(Text, default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    verification_json: Mapped[str] = mapped_column(Text, default="null")
    length: Mapped[str] = mapped_column(String(20))
    engine: Mapped[str] = mapped_column(String(20))
    engine_label: Mapped[str] = mapped_column(String(60))
    original_characters: Mapped[int] = mapped_column(Integer)
    summary_characters: Mapped[int] = mapped_column(Integer)
    compression_ratio: Mapped[str] = mapped_column(String(20))
    processing_ms: Mapped[int] = mapped_column(Integer)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SummaryRequest(StrictPayload):
    title: str = Field(default="", max_length=180)
    content: str
    length: Literal["brief", "standard", "detailed"] = "standard"
    engine: Literal["local", "ai"] = "local"
    selection_constraints: SelectionConstraintsPayload = Field(
        default_factory=lambda: SelectionConstraintsPayload()
    )


class SummaryComparisonRequest(StrictPayload):
    title: str = Field(default="", max_length=180)
    content: str
    selection_constraints: SelectionConstraintsPayload = Field(
        default_factory=lambda: SelectionConstraintsPayload()
    )


class SelectionConstraintsPayload(StrictPayload):
    pinned_sentence_ids: list[int] = Field(default_factory=list, max_length=6)
    excluded_sentence_ids: list[int] = Field(default_factory=list, max_length=180)

    @model_validator(mode="after")
    def validate_constraints(self):
        if len(set(self.pinned_sentence_ids)) != len(self.pinned_sentence_ids):
            raise ValueError("固定句索引不能重复")
        if len(set(self.excluded_sentence_ids)) != len(self.excluded_sentence_ids):
            raise ValueError("排除句索引不能重复")
        if set(self.pinned_sentence_ids) & set(self.excluded_sentence_ids):
            raise ValueError("同一句不能同时被固定和排除")
        return self


class HistoryFact(StrictPayload):
    kind: Literal["subject", "time", "location", "event", "number", "impact"]
    label: str = Field(min_length=1, max_length=20)
    value: str = Field(min_length=1, max_length=8000)
    evidence_sentence_ids: list[int] = Field(min_length=1, max_length=6)
    method: Literal["local_rule"]


class HistoryBullet(StrictPayload):
    text: str = Field(min_length=1, max_length=8000)
    source_sentence_ids: list[int] = Field(min_length=1, max_length=6)


class HistorySourceSentence(StrictPayload):
    id: int = Field(ge=0, le=179)
    text: str = Field(min_length=1, max_length=8000)
    selected: bool
    score: float = Field(ge=0)


class HistoryMetrics(StrictPayload):
    original_characters: int = Field(ge=1, le=8000)
    summary_characters: int = Field(ge=1, le=8000)
    compression_ratio: float = Field(ge=0)
    sentence_count: int | None = Field(default=None, ge=1, le=180)


class HistoryQuality(StrictPayload):
    evidence_coverage: int = Field(ge=0, le=100)
    redundancy_risk: int | None = Field(default=None, ge=0, le=100)
    selected_sentence_count: int = Field(ge=1, le=6)
    source_sentence_count: int = Field(ge=2, le=180)
    fact_coverage: int = Field(default=0, ge=0, le=100)
    facts_found: int = Field(default=0, ge=0, le=6)
    facts_covered: int = Field(default=0, ge=0, le=6)


class VerificationClaimPayload(StrictPayload):
    id: str = Field(min_length=3, max_length=80)
    kind: Literal["subject", "time", "location", "event", "number", "impact"]
    label: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=8000)
    source_sentence_ids: list[int] = Field(min_length=1, max_length=6)
    status: Literal["supported", "partial", "unverified", "conflicting", "offline_only"]
    evidence_source_ids: list[str] = Field(default_factory=list, max_length=6)


class VerificationSourcePayload(StrictPayload):
    id: str = Field(min_length=3, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    url: str = Field(min_length=8, max_length=2048)
    domain: str = Field(min_length=1, max_length=255)
    tier: Literal["official", "established_media", "other"]
    excerpt: str = Field(min_length=1, max_length=280)
    retrieved_at: str = Field(min_length=10, max_length=40)
    content_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("核验来源必须使用完整的 HTTPS 地址")
        return value


class VerificationResultPayload(StrictPayload):
    mode: Literal["offline", "online"]
    status: Literal["completed", "partial", "unavailable"]
    claims: list[VerificationClaimPayload] = Field(max_length=5)
    sources: list[VerificationSourcePayload] = Field(default_factory=list, max_length=MAX_SOURCES)
    searched_at: str | None = Field(default=None, max_length=40)
    notice: str | None = Field(default=None, max_length=300)
    ai_review: AIEvidenceReview | None = None

    @model_validator(mode="after")
    def validate_evidence_references(self):
        source_ids = {source.id for source in self.sources}
        for claim in self.claims:
            if not set(claim.evidence_source_ids).issubset(source_ids):
                raise ValueError("核验主张引用了不存在的外部来源")
        if self.ai_review:
            claim_ids = {claim.id for claim in self.claims}
            review_ids = [review.claim_id for review in self.ai_review.claims]
            if set(review_ids) != claim_ids or len(review_ids) != len(set(review_ids)):
                raise ValueError("AI 解读必须覆盖且只能覆盖当前核验主张")
            sources_by_id = {source.id: source for source in self.sources}
            for review in self.ai_review.claims:
                cited = set(review.evidence_source_ids)
                if not cited.issubset(source_ids):
                    raise ValueError("AI 解读引用了不存在的外部来源")
                if review.suggested_status != "unverified" and not cited:
                    raise ValueError("有倾向的 AI 建议必须引用来源")
                if review.suggested_status in {"supported", "conflicting"} and not any(
                    sources_by_id[source_id].tier in {"official", "established_media"}
                    for source_id in cited
                ):
                    raise ValueError("高可信 AI 建议必须引用官方或主流媒体来源")
        return self


class HistorySummaryResult(StrictPayload):
    title: str = Field(default="", max_length=180)
    summary: str = Field(min_length=1, max_length=8000)
    bullets: list[HistoryBullet] = Field(min_length=1, max_length=6)
    keywords: list[str] = Field(min_length=1, max_length=8)
    source_sentences: list[HistorySourceSentence] = Field(min_length=2, max_length=180)
    selected_sentence_ids: list[int] = Field(min_length=1, max_length=6)
    metrics: HistoryMetrics
    quality: HistoryQuality
    engine: Literal["local", "ai"]
    engine_label: str = Field(min_length=1, max_length=60)
    processing_ms: int = Field(ge=0, le=120000)
    fallback_reason: str | None = Field(default=None, max_length=240)
    facts: list[HistoryFact] = Field(default_factory=list, max_length=6)
    selection_constraints: SelectionConstraintsPayload = Field(
        default_factory=SelectionConstraintsPayload
    )
    verification: VerificationResultPayload | None = None

    @model_validator(mode="after")
    def validate_source_references(self):
        source_ids = {sentence.id for sentence in self.source_sentences}
        if len(source_ids) != len(self.source_sentences):
            raise ValueError("原文句索引不能重复")
        if not set(self.selected_sentence_ids).issubset(source_ids):
            raise ValueError("摘要句索引必须存在于原文依据中")
        for bullet in self.bullets:
            if not set(bullet.source_sentence_ids).issubset(source_ids):
                raise ValueError("要点依据索引必须存在于原文依据中")
        for fact in self.facts:
            if not set(fact.evidence_sentence_ids).issubset(source_ids):
                raise ValueError("事实依据索引必须存在于原文依据中")
        if self.verification:
            for claim in self.verification.claims:
                if not set(claim.source_sentence_ids).issubset(source_ids):
                    raise ValueError("核验主张依据索引必须存在于原文依据中")
        return self


class HistoryCreateRequest(StrictPayload):
    title: str = Field(default="", max_length=180)
    content: str = Field(min_length=80, max_length=8000)
    source_url: str | None = Field(default=None, max_length=2048)
    source_domain: str | None = Field(default=None, max_length=255)
    length: Literal["brief", "standard", "detailed"]
    result: HistorySummaryResult

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        parsed = urlparse(cleaned)
        if not cleaned or parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("来源链接必须是有效的 HTTPS 公开地址")
        return cleaned

    @field_validator("source_domain")
    @classmethod
    def normalize_source_domain(cls, value: str | None) -> str | None:
        return value.strip().lower() if value and value.strip() else None


class ArticleImportRequest(StrictPayload):
    url: str = Field(min_length=8, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_article_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlparse(cleaned)
        if not cleaned or parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("请输入有效的 HTTPS 新闻链接")
        return cleaned


class HistoryImportRecord(HistoryCreateRequest):
    favorite: bool = False
    rating: int | None = Field(default=None, ge=1, le=5)
    created_at: datetime


class HistoryImportRequest(StrictPayload):
    format_version: Literal[1]
    exported_at: datetime
    records: list[HistoryImportRecord] = Field(min_length=1, max_length=200)


class HistoryUpdateRequest(StrictPayload):
    favorite: bool | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    verification: VerificationResultPayload | None = None


class VerificationRequest(StrictPayload):
    title: str = Field(default="", max_length=180)
    content: str = Field(min_length=80, max_length=8000)


class AIEvidenceReviewRequest(StrictPayload):
    verification: VerificationResultPayload


class SearchConfigRequest(StrictPayload):
    provider: Literal["bocha", "brave"] = "bocha"
    api_key: str = Field(min_length=10, max_length=512)

    @field_validator("api_key")
    @classmethod
    def validate_search_api_key(cls, value: str) -> str:
        cleaned = value.strip()
        if any(character in cleaned for character in "\r\n") or len(cleaned) < 10:
            raise ValueError("公开来源检索 API Key 格式无效")
        return cleaned


class AIBullet(StrictPayload):
    text: str = Field(min_length=4, max_length=240)
    source_sentence_ids: list[int] = Field(min_length=1, max_length=4)


class AIResponse(StrictPayload):
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=1000)
    bullets: list[AIBullet] = Field(min_length=1, max_length=6)
    keywords: list[str] = Field(min_length=3, max_length=8)


class AIConfigRequest(StrictPayload):
    api_key: str = Field(min_length=10, max_length=512)
    base_url: str = Field(default=AI_ENV_DEFAULTS["AI_BASE_URL"], min_length=8, max_length=240)
    model: str = Field(default=AI_ENV_DEFAULTS["AI_MODEL"], min_length=2, max_length=120)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        cleaned = value.strip()
        if any(character in cleaned for character in "\r\n"):
            raise ValueError("API Key 不能包含换行符")
        if len(cleaned) < 10:
            raise ValueError("API Key 至少需要 10 个字符")
        return cleaned

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("服务地址应为完整的 http 或 https 地址")
        return cleaned

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        cleaned = value.strip()
        if any(character in cleaned for character in "\r\n"):
            raise ValueError("模型名称不能包含换行符")
        return cleaned


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def evidence_coverage(bullets: list[dict], valid_ids: set[int]) -> int:
    if not bullets:
        return 0
    supported = sum(
        bool(item.get("source_sentence_ids"))
        and set(item["source_sentence_ids"]).issubset(valid_ids)
        for item in bullets
    )
    return round(supported / len(bullets) * 100)


def fallback_quality(
    bullets: list[dict],
    source_sentences: list[dict],
    selected_ids: list[int],
    facts: list[dict] | None = None,
) -> dict:
    valid_ids = {item["id"] for item in source_sentences}
    return {
        "evidence_coverage": evidence_coverage(bullets, valid_ids),
        "redundancy_risk": None,
        "selected_sentence_count": len(selected_ids),
        "source_sentence_count": len(source_sentences),
        **fact_quality(facts or [], selected_ids),
    }


def serialize_record(record: HistoryRecord) -> dict:
    bullets = json.loads(record.bullets_json)
    source_sentences = json.loads(record.source_sentences_json)
    selected_ids = json.loads(record.selected_ids_json)
    try:
        facts = json.loads(record.facts_json or "[]")
    except json.JSONDecodeError:
        facts = []
    try:
        constraints = json.loads(record.constraints_json or "{}")
    except json.JSONDecodeError:
        constraints = {}
    try:
        verification = json.loads(record.verification_json or "null")
    except json.JSONDecodeError:
        verification = None
    if verification is None:
        verification = build_offline_verification(facts, source_sentences)
    try:
        quality = json.loads(record.quality_json or "{}")
    except json.JSONDecodeError:
        quality = {}
    quality = {**fallback_quality(bullets, source_sentences, selected_ids, facts), **quality}
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "source_url": record.source_url,
        "source_domain": record.source_domain,
        "summary": record.summary,
        "bullets": bullets,
        "keywords": json.loads(record.keywords_json),
        "selected_sentence_ids": selected_ids,
        "source_sentences": source_sentences,
        "quality": quality,
        "facts": facts,
        "selection_constraints": {
            "pinned_sentence_ids": constraints.get("pinned_sentence_ids", []),
            "excluded_sentence_ids": constraints.get("excluded_sentence_ids", []),
        },
        "verification": verification,
        "length": record.length,
        "engine": record.engine,
        "engine_label": record.engine_label,
        "metrics": {
            "original_characters": record.original_characters,
            "summary_characters": record.summary_characters,
            "compression_ratio": float(record.compression_ratio),
        },
        "processing_ms": record.processing_ms,
        "favorite": record.favorite,
        "rating": record.rating,
        "created_at": record.created_at.isoformat(),
    }


def create_history_record(
    request: HistoryCreateRequest,
    *,
    favorite: bool = False,
    rating: int | None = None,
    created_at: datetime | None = None,
) -> HistoryRecord:
    result = request.result
    return HistoryRecord(
        title=result.title or request.title,
        content=request.content,
        source_url=request.source_url,
        source_domain=request.source_domain,
        summary=result.summary,
        bullets_json=json.dumps(
            [bullet.model_dump() for bullet in result.bullets], ensure_ascii=False
        ),
        keywords_json=json.dumps(result.keywords, ensure_ascii=False),
        selected_ids_json=json.dumps(result.selected_sentence_ids),
        source_sentences_json=json.dumps(
            [sentence.model_dump() for sentence in result.source_sentences], ensure_ascii=False
        ),
        quality_json=json.dumps(result.quality.model_dump()),
        facts_json=json.dumps([fact.model_dump() for fact in result.facts], ensure_ascii=False),
        constraints_json=json.dumps(result.selection_constraints.model_dump()),
        verification_json=json.dumps(
            result.verification.model_dump() if result.verification else None,
            ensure_ascii=False,
        ),
        length=request.length,
        engine=result.engine,
        engine_label=result.engine_label,
        original_characters=result.metrics.original_characters,
        summary_characters=result.metrics.summary_characters,
        compression_ratio=str(result.metrics.compression_ratio),
        processing_ms=result.processing_ms,
        favorite=favorite,
        rating=rating,
        created_at=created_at or datetime.now(),
    )


def serialize_backup_record(record: HistoryRecord) -> dict:
    serialized = serialize_record(record)
    result_keys = (
        "title",
        "summary",
        "bullets",
        "keywords",
        "source_sentences",
        "selected_sentence_ids",
        "metrics",
        "quality",
        "engine",
        "engine_label",
        "processing_ms",
        "facts",
        "selection_constraints",
        "verification",
    )
    return {
        "title": serialized["title"],
        "content": serialized["content"],
        "source_url": serialized["source_url"],
        "source_domain": serialized["source_domain"],
        "length": serialized["length"],
        "favorite": serialized["favorite"],
        "rating": serialized["rating"],
        "created_at": serialized["created_at"],
        "result": {key: serialized[key] for key in result_keys},
    }


def load_ai_environment() -> None:
    """Load local connection settings without overriding process variables."""
    if not AI_ENV_PATH.exists():
        return
    for line in AI_ENV_PATH.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        supported = {"AI_API_KEY", "SEARCH_API_KEY", *AI_ENV_DEFAULTS, *SEARCH_ENV_DEFAULTS}
        if separator and key in supported and key not in os.environ:
            os.environ[key] = value.strip()


def _save_environment_values(values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if AI_ENV_PATH.exists():
        for line in AI_ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip():
                existing[key.strip()] = value.strip()
    existing.update(values)
    supported_order = [
        "AI_API_KEY",
        "AI_BASE_URL",
        "AI_MODEL",
        "SEARCH_PROVIDER",
        "SEARCH_API_KEY",
    ]
    ordered = [key for key in supported_order if key in existing]
    ordered.extend(key for key in existing if key not in ordered)
    AI_ENV_PATH.write_text(
        "\n".join(f"{key}={existing[key]}" for key in ordered) + "\n", encoding="utf-8"
    )
    os.environ.update(values)


def ai_settings() -> dict:
    configured = ai_configured()
    return {
        "enabled": configured,
        "label": "AI 增强摘要",
        "message": "已配置，可在工作台选择 AI 增强。"
        if configured
        else "尚未配置 API Key，可在下方填写后立即启用。",
        "base_url": os.getenv("AI_BASE_URL", AI_ENV_DEFAULTS["AI_BASE_URL"]),
        "model": os.getenv("AI_MODEL", AI_ENV_DEFAULTS["AI_MODEL"]),
    }


def ai_evidence_review_settings() -> dict:
    configured = ai_configured()
    return {
        "enabled": configured,
        "label": "AI 辅助证据解读",
        "message": (
            "已配置，可在完成联网核验后主动解读来源短摘录。"
            if configured
            else "尚未配置 AI 服务；规则核验仍可完整使用。"
        ),
        "requires_online_sources": True,
        "max_sources": MAX_SOURCES,
    }


def save_ai_environment(config: AIConfigRequest) -> None:
    """Persist only AI connection details locally; the key is never returned to clients."""
    values = {
        "AI_API_KEY": config.api_key,
        "AI_BASE_URL": config.base_url,
        "AI_MODEL": config.model,
    }
    _save_environment_values(values)


def ai_configured() -> bool:
    return bool(os.getenv("AI_API_KEY"))


def search_configured() -> bool:
    return bool(os.getenv("SEARCH_API_KEY"))


def configured_search_provider() -> str:
    provider = os.getenv("SEARCH_PROVIDER", SEARCH_ENV_DEFAULTS["SEARCH_PROVIDER"]).strip().lower()
    return provider if provider in SEARCH_PROVIDER_LABELS else SEARCH_ENV_DEFAULTS["SEARCH_PROVIDER"]


def search_settings() -> dict:
    configured = search_configured()
    provider = configured_search_provider()
    return {
        "enabled": configured,
        "provider": provider,
        "provider_label": SEARCH_PROVIDER_LABELS[provider],
        "label": "公开来源核验",
        "message": f"已配置 {SEARCH_PROVIDER_LABELS[provider]}，可在核验线索中主动检索公开来源。"
        if configured
        else f"尚未配置 {SEARCH_PROVIDER_LABELS[provider]} API Key，仍可使用离线核验线索。",
        "max_sources": MAX_SOURCES,
    }


def save_search_environment(config: SearchConfigRequest) -> None:
    _save_environment_values(
        {
            "SEARCH_PROVIDER": config.provider,
            "SEARCH_API_KEY": config.api_key,
        }
    )


load_ai_environment()


def verify_ai_connection(config: AIConfigRequest) -> dict:
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    response = client.chat.completions.create(
        model=config.model,
        messages=[{"role": "user", "content": "Reply with OK."}],
        temperature=0,
        max_tokens=4,
        timeout=8,
    )
    return {
        "available": True,
        "model": response.model or config.model,
        "message": "连接成功，模型可以响应。",
    }


def verify_error_message(error: Exception) -> str:
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return "API Key 验证失败，请检查密钥权限。"
    if status_code == 404:
        return "未找到服务或模型，请检查服务地址和模型名称。"
    if "timeout" in type(error).__name__.lower():
        return "连接超时，请检查网络后重试。"
    return "暂时无法连接 AI 服务，请检查网络、服务地址和模型名称。"


def evidence_review_error_message(error: Exception) -> str:
    if isinstance(error, EvidenceReviewError):
        return str(error)
    return "AI 证据解读暂不可用，已保留规则核验结果。"


def search_verify_error_message(error: Exception) -> str:
    status_code = getattr(error, "response", None)
    status_code = getattr(status_code, "status_code", None)
    if status_code in {401, 403}:
        return "搜索 API Key 验证失败，请检查密钥权限。"
    if status_code == 429:
        return "搜索服务当前请求过多，请稍后再试。"
    if "timeout" in type(error).__name__.lower():
        return "搜索服务连接超时，请检查网络后重试。"
    return "暂时无法连接公开来源检索服务，请检查网络和 API Key。"


def run_ai_summary(request: SummaryRequest, local_result: dict) -> dict:
    if not ai_configured():
        raise RuntimeError("AI 服务尚未配置")
    source_lines = "\n".join(
        f"[{sentence['id']}] {sentence['text']}" for sentence in local_result["source_sentences"]
    )
    target = {"brief": "2", "standard": "3-4", "detailed": "5-6"}[request.length]
    prompt = f"""你是一名中文新闻编辑。只能使用下列新闻原文，不得补充外部事实。
请输出严格 JSON：{{\"title\":\"...\",\"summary\":\"...\",\"bullets\":[{{\"text\":\"...\",\"source_sentence_ids\":[0]}}],\"keywords\":[\"...\"]}}。
需要 {target} 条要点，每条要点必须给出一个或多个支持它的原文句号索引。
新闻标题：{request.title or "未提供"}
原文句子：
{source_lines}"""
    client = OpenAI(
        api_key=os.environ["AI_API_KEY"],
        base_url=os.getenv("AI_BASE_URL", "https://api.deepseek.com"),
    )
    response = client.chat.completions.create(
        model=os.getenv("AI_MODEL", "deepseek-chat"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},
        timeout=18,
    )
    content = response.choices[0].message.content or "{}"
    parsed = AIResponse.model_validate_json(content)
    valid_ids = {sentence["id"] for sentence in local_result["source_sentences"]}
    for bullet in parsed.bullets:
        if not set(bullet.source_sentence_ids).issubset(valid_ids):
            raise ValueError("AI 返回了无效的原文依据索引")
    selected_ids = sorted(
        {item for bullet in parsed.bullets for item in bullet.source_sentence_ids}
    )
    source_sentences = [
        {**sentence, "selected": sentence["id"] in selected_ids}
        for sentence in local_result["source_sentences"]
    ]
    metrics = dict(local_result["metrics"])
    metrics["summary_characters"] = len(parsed.summary)
    metrics["compression_ratio"] = round(
        len(parsed.summary) / metrics["original_characters"] * 100, 1
    )
    quality = dict(local_result["quality"])
    quality.update(
        {
            "evidence_coverage": evidence_coverage(
                [item.model_dump() for item in parsed.bullets], valid_ids
            ),
            "redundancy_risk": calculate_redundancy_risk([item.text for item in parsed.bullets]),
            "selected_sentence_count": len(selected_ids),
            "source_sentence_count": len(local_result["source_sentences"]),
        }
    )
    result = dict(local_result)
    result.update(
        {
            "title": parsed.title,
            "summary": parsed.summary,
            "bullets": [item.model_dump() for item in parsed.bullets],
            "keywords": parsed.keywords,
            "selected_sentence_ids": selected_ids,
            "source_sentences": source_sentences,
            "metrics": metrics,
            "quality": quality,
            "engine": "ai",
            "engine_label": "AI 增强摘要",
            "fallback_reason": None,
        }
    )
    return result


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    history_columns = {column["name"] for column in inspect(engine).get_columns("history_records")}
    if "quality_json" not in history_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE history_records ADD COLUMN quality_json TEXT DEFAULT '{}'")
            )
    if "facts_json" not in history_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE history_records ADD COLUMN facts_json TEXT DEFAULT '[]'")
            )
    if "constraints_json" not in history_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE history_records ADD COLUMN constraints_json TEXT DEFAULT '{}'")
            )
    if "verification_json" not in history_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE history_records ADD COLUMN verification_json TEXT DEFAULT 'null'")
            )
    if "source_url" not in history_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE history_records ADD COLUMN source_url TEXT"))
    if "source_domain" not in history_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE history_records ADD COLUMN source_domain TEXT"))
    yield


api_router = APIRouter()


@api_router.get("/api/health")
def health():
    return {"status": "ok"}


@api_router.get("/api/capabilities")
def capabilities():
    return {
        "local_engine": {"enabled": True, "label": "本地可靠摘要"},
        "ai_engine": ai_settings(),
        "ai_evidence_review": ai_evidence_review_settings(),
        "verification_engine": search_settings(),
        "limits": {"min_characters": 80, "max_characters": 8000, "max_sentences": 180},
    }


@api_router.put("/api/ai-config")
def update_ai_config(config: AIConfigRequest):
    save_ai_environment(config)
    return capabilities()


@api_router.post("/api/ai-config/verify")
def verify_ai_config(config: AIConfigRequest):
    try:
        return verify_ai_connection(config)
    except Exception as error:
        return {"available": False, "model": config.model, "message": verify_error_message(error)}


@api_router.put("/api/search-config")
def update_search_config(config: SearchConfigRequest):
    save_search_environment(config)
    return capabilities()


@api_router.post("/api/search-config/verify")
async def verify_search_config(config: SearchConfigRequest):
    try:
        return await verify_search_connection(config.provider, config.api_key)
    except Exception as error:
        return {
            "available": False,
            "provider": config.provider,
            "message": search_verify_error_message(error),
        }


@api_router.get("/api/samples")
def samples():
    return SAMPLES


def trim_imported_content(text_value: str) -> str:
    """Keep extracted paragraphs within the same input limit as manual articles."""
    paragraphs = [paragraph.strip() for paragraph in text_value.splitlines() if paragraph.strip()]
    if not paragraphs:
        return ""
    selected: list[str] = []
    current_length = 0
    for paragraph in paragraphs:
        separator_length = 1 if selected else 0
        remaining = MAX_ARTICLE_CONTENT_CHARACTERS - current_length - separator_length
        if remaining <= 0:
            break
        if len(paragraph) > remaining:
            selected.append(paragraph[:remaining].rstrip())
            break
        selected.append(paragraph)
        current_length += len(paragraph) + separator_length
    return "\n".join(selected)


def media_resource_kind(url: str) -> str | None:
    """Identify direct media files without blocking HTML news pages that contain media."""
    path = urlparse(url).path.lower()
    for kind, extensions in MEDIA_RESOURCE_EXTENSIONS.items():
        if any(path.endswith(extension) for extension in extensions):
            return kind
    return None


def is_known_dynamic_article_url(url: str) -> bool:
    """Recognize publishers whose article content is assembled only after page scripts run."""
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == host or hostname.endswith(f".{host}") for host in KNOWN_DYNAMIC_ARTICLE_HOSTS)


@api_router.post("/api/articles/import")
async def import_article(request: ArticleImportRequest):
    resource_kind = media_resource_kind(request.url)
    if resource_kind == "image":
        raise HTTPException(
            status_code=422,
            detail="该链接是图片资源，无法提取新闻正文。请提供对应新闻报道网页链接或手动粘贴文字稿。",
        )
    if resource_kind == "video":
        raise HTTPException(
            status_code=422,
            detail="该链接是视频资源，无法提取新闻正文。请提供对应新闻报道网页链接或手动粘贴文字稿。",
        )
    try:
        async with httpx.AsyncClient() as client:
            article = await fetch_public_page(client, request.url)
    except (httpx.HTTPError, ValueError):
        article = None
    if not article:
        if is_known_dynamic_article_url(request.url):
            raise HTTPException(
                status_code=422,
                detail=(
                    "该 MSN 新闻页需要在浏览器中动态加载正文，当前无法可靠提取。"
                    "请粘贴原始发布媒体的报道链接，或手动复制标题和文字正文。"
                ),
            )
        raise HTTPException(
            status_code=422,
            detail="无法提取该链接的公开新闻正文，请确认链接可直接访问，或手动粘贴正文。",
        )
    content = trim_imported_content(article["text"])
    if len(content.replace(" ", "").replace("\n", "")) < 80:
        raise HTTPException(
            status_code=422,
            detail="该页面未提供足够的可用正文，请手动粘贴新闻内容。",
        )
    return {
        "title": article["title"],
        "content": content,
        "source_url": article["url"],
        "source_domain": article["domain"],
        "retrieved_at": datetime.now().isoformat(),
    }


@api_router.get("/api/benchmarks/overview")
def get_benchmark_overview():
    return benchmark_overview()


@api_router.post("/api/benchmarks/run")
def run_benchmark():
    try:
        return run_private_benchmark()
    except BenchmarkUnavailable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@api_router.post("/api/summaries")
async def create_summary(request: SummaryRequest):
    started = time.perf_counter()
    try:
        local_result = summarize(
            request.title,
            request.content,
            request.length,
            request.selection_constraints.model_dump(),
        )
    except SummarizationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    local_result["verification"] = build_offline_verification(
        local_result["facts"], local_result["source_sentences"]
    )
    result = local_result
    if request.engine == "ai":
        try:
            result = await asyncio.to_thread(run_ai_summary, request, local_result)
        except Exception:
            # The source-traceable local result remains the safe product fallback.
            result = dict(local_result)
            result["fallback_reason"] = "AI 增强不可用，已切换为本地可靠摘要。"
    result["processing_ms"] = int((time.perf_counter() - started) * 1000)
    return result


@api_router.post("/api/verifications")
async def create_verification(request: VerificationRequest):
    try:
        if not search_configured():
            return offline_verification_for_article(request.title, request.content)
        return await run_online_verification(
            request.title,
            request.content,
            os.environ.get("SEARCH_API_KEY", ""),
            provider=get_search_provider(configured_search_provider()),
        )
    except SummarizationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@api_router.post("/api/verifications/ai-review")
async def create_ai_evidence_review(request: AIEvidenceReviewRequest):
    if not ai_configured():
        raise HTTPException(status_code=409, detail="请先在设置中配置 AI 服务")
    verification = request.verification.model_dump()
    if verification["mode"] != "online" or not verification["sources"]:
        raise HTTPException(status_code=422, detail="请先完成包含公开来源的联网核验")
    try:
        review = await asyncio.to_thread(
            review_evidence_with_ai,
            verification,
            api_key=os.environ.get("AI_API_KEY", ""),
            base_url=os.getenv("AI_BASE_URL", AI_ENV_DEFAULTS["AI_BASE_URL"]),
            model=os.getenv("AI_MODEL", AI_ENV_DEFAULTS["AI_MODEL"]),
        )
    except EvidenceReviewError as error:
        raise HTTPException(status_code=422, detail=evidence_review_error_message(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=evidence_review_error_message(error)) from error
    try:
        return VerificationResultPayload.model_validate(
            {**verification, "ai_review": review}
        ).model_dump()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="AI 解读返回格式或引用不符合要求") from error


@api_router.post("/api/summaries/compare")
def create_summary_comparison(request: SummaryComparisonRequest):
    started = time.perf_counter()
    try:
        variants = compare_summaries(
            request.title,
            request.content,
            request.selection_constraints.model_dump(),
        )
    except SummarizationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    processing_ms = int((time.perf_counter() - started) * 1000)
    results = []
    for length in ("brief", "standard", "detailed"):
        result = variants[length]
        result["verification"] = build_offline_verification(
            result["facts"], result["source_sentences"]
        )
        results.append({"length": length, "result": {**result, "processing_ms": processing_ms}})
    return {
        "processing_ms": processing_ms,
        "results": results,
    }


@api_router.get("/api/history")
def get_history(
    search: str = Query(default="", max_length=120),
    favorite: bool | None = None,
    sort: Literal["latest", "rating"] = "latest",
    session: Session = Depends(get_session),
):
    query = session.query(HistoryRecord)
    if search.strip():
        pattern = f"%{search.strip()}%"
        query = query.filter(
            or_(HistoryRecord.title.like(pattern), HistoryRecord.summary.like(pattern))
        )
    if favorite is not None:
        query = query.filter(HistoryRecord.favorite.is_(favorite))
    if sort == "rating":
        records = query.order_by(
            case((HistoryRecord.rating.is_(None), 1), else_=0),
            desc(HistoryRecord.rating),
            desc(HistoryRecord.created_at),
        ).all()
    else:
        records = query.order_by(desc(HistoryRecord.created_at)).all()
    return [serialize_record(record) for record in records]


@api_router.get("/api/history/backup")
def export_history_backup(session: Session = Depends(get_session)):
    records = session.query(HistoryRecord).order_by(desc(HistoryRecord.created_at)).all()
    return {
        "format_version": 1,
        "exported_at": datetime.now().isoformat(),
        "records": [serialize_backup_record(record) for record in records],
    }


@api_router.post("/api/history/import")
def import_history_backup(request: HistoryImportRequest, session: Session = Depends(get_session)):
    existing_fingerprints = {
        (record.content, record.summary) for record in session.query(HistoryRecord).all()
    }
    imported = 0
    skipped = 0
    for item in request.records:
        fingerprint = (item.content, item.result.summary)
        if fingerprint in existing_fingerprints:
            skipped += 1
            continue
        session.add(
            create_history_record(
                item,
                favorite=item.favorite,
                rating=item.rating,
                created_at=item.created_at,
            )
        )
        existing_fingerprints.add(fingerprint)
        imported += 1
    session.commit()
    return {"imported": imported, "skipped": skipped}


@api_router.post("/api/history", status_code=201)
def save_history(request: HistoryCreateRequest, session: Session = Depends(get_session)):
    record = create_history_record(request)
    session.add(record)
    session.commit()
    session.refresh(record)
    return serialize_record(record)


@api_router.patch("/api/history/{record_id}")
def update_history(
    record_id: int, request: HistoryUpdateRequest, session: Session = Depends(get_session)
):
    record = session.get(HistoryRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到该历史记录。")
    if request.favorite is not None:
        record.favorite = request.favorite
    if request.rating is not None:
        record.rating = request.rating
    if request.verification is not None:
        record.verification_json = json.dumps(request.verification.model_dump(), ensure_ascii=False)
    session.commit()
    session.refresh(record)
    return serialize_record(record)


@api_router.delete("/api/history/{record_id}", status_code=204)
def delete_history(record_id: int, session: Session = Depends(get_session)):
    record = session.get(HistoryRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到该历史记录。")
    session.delete(record)
    session.commit()


@api_router.delete("/api/history", status_code=204)
def clear_history(session: Session = Depends(get_session)):
    session.query(HistoryRecord).delete()
    session.commit()


def create_app(
    database_url: str | None = None,
    *,
    serve_frontend: bool = True,
) -> FastAPI:
    """Create a runnable app with an optional isolated SQLite target."""
    if database_url:
        configure_database(database_url)
    application = FastAPI(title="NewsBrief API", version="2.4.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(api_router)
    if serve_frontend and FRONTEND_DIST.exists():
        # Production demos use the Vite build from one local FastAPI process.
        application.mount(
            "/", StaticFiles(directory=FRONTEND_DIST, html=True), name="newsbrief-frontend"
        )
    return application


app = create_app()
