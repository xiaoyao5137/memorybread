"""创作服务 - 基于本地文档资产的加权 RAG 创作流水线。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional
from urllib.parse import quote_plus, urlparse
from uuid import uuid4

import httpx

from .tools import (
    CreationToolExecutionError,
    DATA_SEARCH_TOOL_ID,
    INTERNET_SEARCH_TOOL_ID,
    MEMORY_SEARCH_TOOL_ID,
    WEBPAGE_SCRAPE_TOOL_ID,
    fallback_routing_decision,
    normalize_creation_tool_ids,
    routing_capability_lines,
    validate_routing_decision,
)

logger = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
CORE_ENGINE_DEFAULT_BASE_URL = "http://127.0.0.1:7070"
MAX_REPORT_REFRESH_SOURCES = 5

CREATION_SKILL_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "skill_description": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "purpose": {"type": "string"},
                "document_types": {"type": "array", "items": {"type": "string"}},
                "problems": {"type": "array", "items": {"type": "string"}},
                "domains": {"type": "array", "items": {"type": "string"}},
                "deliverables": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "purpose",
                "document_types",
                "problems",
                "domains",
                "deliverables",
            ],
        },
        "execution_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "output": {"type": "string"},
                    "agents": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "tools": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "id",
                    "title",
                    "objective",
                    "output",
                    "agents",
                    "skills",
                    "tools",
                ],
            },
        },
        "common_titles": {"type": "array", "items": {"type": "string"}},
        "title_style": {"type": "string"},
        "text_style": {"type": "string"},
        "diagram_style": {"type": "string"},
        "writing_guidelines": {"type": "array", "items": {"type": "string"}},
        "distinctive_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "guidance": {"type": "string"},
                    "examples": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "description", "guidance", "examples"],
            },
        },
        "section_headings": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "common_titles": {"type": "string"},
                "title_style": {"type": "string"},
                "text_style": {"type": "string"},
                "diagram_style": {"type": "string"},
                "writing_guidelines": {"type": "string"},
            },
            "required": [
                "common_titles",
                "title_style",
                "text_style",
                "diagram_style",
                "writing_guidelines",
            ],
        },
        "field_examples": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "common_titles": {"type": "array", "items": {"type": "string"}},
                "title_style": {"type": "array", "items": {"type": "string"}},
                "text_style": {"type": "array", "items": {"type": "string"}},
                "diagram_style": {"type": "array", "items": {"type": "string"}},
                "writing_guidelines": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "common_titles",
                "title_style",
                "text_style",
                "diagram_style",
                "writing_guidelines",
            ],
        },
        "example_document": {"type": "string"},
        "suggested_category_keywords": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "title",
        "summary",
        "skill_description",
        "execution_steps",
        "common_titles",
        "title_style",
        "text_style",
        "diagram_style",
        "writing_guidelines",
        "distinctive_sections",
        "section_headings",
        "field_examples",
        "example_document",
        "suggested_category_keywords",
    ],
}


@dataclass
class CreationOptions:
    """本次创作的控制参数。"""

    doc_type: str = ""
    audience: str = ""
    output_format: str = "markdown"
    inherit_format: bool = True
    enable_rag: bool = True
    enable_web_search: bool = False
    enable_image_generation: bool = False
    content_weight: float = 0.45
    quality_weight: float = 0.15
    completeness_weight: float = 0.15
    usage_weight: float = 0.10
    format_weight: float = 0.10
    freshness_weight: float = 0.05
    max_references: int = 10
    data_search_limit: int = 30
    enabled_tools: tuple[str, ...] = (
        INTERNET_SEARCH_TOOL_ID,
        MEMORY_SEARCH_TOOL_ID,
        DATA_SEARCH_TOOL_ID,
        WEBPAGE_SCRAPE_TOOL_ID,
    )

    def __post_init__(self) -> None:
        self.enabled_tools = normalize_creation_tool_ids(self.enabled_tools)
        self.max_references = max(1, min(int(self.max_references), 30))
        self.data_search_limit = max(1, min(int(self.data_search_limit), 50))
        # 旧布尔字段继续保留，但其值投影自新 Tool 契约，不能关闭必备 Tool。
        self.enable_rag = MEMORY_SEARCH_TOOL_ID in self.enabled_tools
        self.enable_web_search = INTERNET_SEARCH_TOOL_ID in self.enabled_tools


@dataclass
class ReferenceDocument:
    id: int
    title: str
    doc_type: str
    summary: str
    full_content: str
    sections_json: str
    style_phrases: str
    prompt_hint: str
    usage_count: int
    review_status: str
    updated_at: int
    source_url: Optional[str]
    relevance_score: float
    quality_score: float
    completeness_score: float
    usage_score: float
    format_score: float
    freshness_score: float
    final_weight: float
    reason: str


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class GithubSearchResult:
    full_name: str
    url: str
    description: str
    stars: int
    language: str
    updated_at: str


class CreationService:
    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        db_path: Optional[str] = None,
        model: Optional[str] = None,
        enable_vector_recall: bool = True,
    ):
        self.ollama_base_url = ollama_base_url
        if model is None:
            from model_registry_global import get_active_ollama_model
            model = get_active_ollama_model()
        self.model = model
        self.db_path = db_path or str(Path.home() / ".memory-bread" / "memory-bread.db")
        self.enable_vector_recall = enable_vector_recall
        self._embedding_model = None
        self._ocr_engine = None
        if enable_vector_recall:
            try:
                from embedding.model import EmbeddingModel
                self._embedding_model = EmbeddingModel.create_default()
                logger.info("向量召回已启用，embedding模型: %s", self._embedding_model.model_name)
            except Exception as e:
                logger.warning("初始化embedding模型失败，将禁用向量召回: %s", e)
                self.enable_vector_recall = False

    @property
    def core_engine_base_url(self) -> str:
        return (
            os.getenv("CORE_ENGINE_URL")
            or os.getenv("MEMORY_BREAD_CORE_URL")
            or CORE_ENGINE_DEFAULT_BASE_URL
        ).rstrip("/")

    async def retrieve_data_context(
        self,
        query: str,
        parsed_requirement: dict,
        limit: int = 30,
    ) -> list[dict]:
        """调用本机数据检索 Tool，返回含时效与可采纳状态的候选。"""
        payload = {
            "query": query,
            "need_fresh": True,
            "limit": max(1, min(int(limit), 50)),
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.core_engine_base_url}/api/tools/data-search",
                    json=payload,
                )
        except Exception as exc:
            raise CreationToolExecutionError(
                "DATA_SEARCH_UNAVAILABLE",
                "本地数据检索暂时不可用",
            ) from exc
        if not response.is_success:
            raise CreationToolExecutionError(
                "DATA_SEARCH_UNAVAILABLE",
                "本地数据检索暂时不可用",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CreationToolExecutionError(
                "DATA_SEARCH_UNAVAILABLE",
                "本地数据检索返回格式无效",
            ) from exc
        results = data.get("results") if isinstance(data, dict) else []
        return [item for item in (results or []) if isinstance(item, dict)][:50]

    async def scrape_data_context(
        self,
        data_results: list[dict],
        query: str,
        parsed_requirement: dict,
        limit: int = MAX_REPORT_REFRESH_SOURCES,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        preview_ids: Optional[dict[int, str]] = None,
        retain_screenshot: bool = True,
    ) -> dict:
        """刷新 Top-K 报表源，以 AX/DOM 校验数据并按需保留截图证据。"""
        report_sources = [
            item
            for item in data_results
            if item.get("source_kind") == "report_url"
            and item.get("source_url")
            and item.get("source_id") is not None
        ][: max(1, min(int(limit), MAX_REPORT_REFRESH_SOURCES))]
        if not report_sources:
            return {"scrapes": [], "refreshed_data": data_results}

        scrapes: list[dict] = []
        payload_by_source: dict[int, dict] = {}
        evidence_by_source: dict[int, dict] = {}
        async with httpx.AsyncClient(timeout=45.0) as client:
            for item in report_sources:
                source_id = int(item["source_id"])
                preview_id = str((preview_ids or {}).get(source_id) or uuid4())
                preview_url = f"/api/creation/browser-previews/{preview_id}/image"
                try:
                    response = await client.post(
                        f"{self.core_engine_base_url}/api/data/sources/{source_id}/refresh",
                        json={
                            "mode": "auto",
                            "capture_evidence": True,
                            "retain_screenshot": bool(retain_screenshot),
                            "run_id": run_id,
                            "session_id": session_id,
                            "preview_id": preview_id,
                        },
                    )
                    if response.is_success:
                        payload = response.json()
                        payload_by_source[source_id] = payload
                        evidence = await self._validate_scrape_evidence(
                            client,
                            payload,
                            require_metric=True,
                        )
                        evidence_by_source[source_id] = evidence
                        verified_claims = (
                            evidence.get("validation", {}).get("verified_claims", [])
                            if isinstance(evidence, dict)
                            else []
                        )
                        scrapes.append(
                            {
                                "source_id": source_id,
                                "status": (
                                    "completed"
                                    if evidence.get("validation_status") == "verified"
                                    else "rejected"
                                ),
                                "collector": payload.get("collector"),
                                "collected_at": payload.get("collected_at"),
                                "title": payload.get("title"),
                                "url": payload.get("url"),
                                "browser": payload.get("browser"),
                                "interaction_mode": payload.get("interaction_mode"),
                                **(
                                    {
                                        "preview_id": preview_id,
                                        "preview_url": preview_url,
                                    }
                                    if retain_screenshot
                                    else {}
                                ),
                                "evidence": evidence,
                                "validation_reason": (
                                    evidence.get("validation", {}).get("reason")
                                    if isinstance(evidence, dict)
                                    else "evidence_missing"
                                ),
                                "verified_claim_count": len(verified_claims),
                            }
                        )
                        continue
                    error_payload = response.json()
                    error_code = str(error_payload.get("error") or "SCRAPE_FAILED")
                except Exception:
                    error_code = "SCRAPE_FAILED"
                scrapes.append(
                    {
                        "source_id": source_id,
                        "status": "failed",
                        "error_code": error_code,
                        **(
                            {"preview_id": preview_id, "preview_url": preview_url}
                            if retain_screenshot
                            else {}
                        ),
                    }
                )

        # 保留最初 Top-K 的身份与顺序，直接把本轮浏览器响应合并回候选。
        # 这里不能重新检索，否则刚刷新的 URL 可能因内容或时间分变化而掉出 Top-K，
        # 最终出现“浏览器打开了页面，但 Writer 看不到这次采集”的断链。
        refreshed = self._merge_scrape_results(
            data_results,
            payload_by_source,
            evidence_by_source,
            {int(item["source_id"]) for item in report_sources},
        )
        return {"scrapes": scrapes, "refreshed_data": refreshed}

    @staticmethod
    def _merge_scrape_results(
        data_results: list[dict],
        payload_by_source: dict[int, dict],
        evidence_by_source: dict[int, dict],
        attempted_source_ids: set[int],
    ) -> list[dict]:
        attempted_report_urls = {
            str(item.get("source_url") or "").strip(): int(item["source_id"])
            for item in data_results
            if item.get("source_kind") == "report_url"
            and item.get("source_id") is not None
            and int(item["source_id"]) in attempted_source_ids
            and str(item.get("source_url") or "").strip()
        }
        merged: list[dict] = []
        for original in data_results:
            item = dict(original)
            source_id_value = item.get("source_id")
            source_id = int(source_id_value) if source_id_value is not None else None
            source_url = str(item.get("source_url") or "").strip()
            live_source_id = attempted_report_urls.get(source_url)
            if item.get("source_kind") != "report_url" and live_source_id is not None:
                # 同 URL 工作记忆是报表的历史派生值。既然本轮已经请求即时刷新，
                # 无论本轮程序化刷新是否可用，都不能再让旧派生值冒充“最新数据”。
                item.update(
                    {
                        "can_use": False,
                        "content_excerpt": None,
                        "structured_data": None,
                        "freshness_class": "superseded",
                        "unavailable_reason": "superseded_by_live_report",
                        "superseded_by_source_id": live_source_id,
                    }
                )
                merged.append(item)
                continue
            payload = payload_by_source.get(source_id) if source_id is not None else None
            evidence = evidence_by_source.get(source_id) if source_id is not None else None
            if payload is not None:
                collected_at = payload.get("collected_at")
                evidence_verified = (
                    isinstance(evidence, dict)
                    and evidence.get("validation_status") == "verified"
                )
                validation = (
                    evidence.get("validation")
                    if isinstance(evidence, dict)
                    and isinstance(evidence.get("validation"), dict)
                    else {}
                )
                item.update(
                    {
                        "title": payload.get("title") or item.get("title"),
                        "source_url": payload.get("url") or item.get("source_url"),
                        "collected_at": collected_at,
                        "observed_at": collected_at,
                        "freshness_class": "fresh" if evidence_verified else "unverified",
                        "freshness_score": 1.0 if evidence_verified else 0.0,
                        "refresh_required": not evidence_verified,
                        "content_excerpt": payload.get("content_text"),
                        "structured_data": payload.get("structured_data"),
                        "evidence_status": (
                            evidence.get("validation_status")
                            if isinstance(evidence, dict)
                            else "rejected"
                        ),
                        "evidence_reason": validation.get("reason") or "evidence_missing",
                        "provenance": {
                            "collector": payload.get("collector"),
                            "browser": payload.get("browser"),
                            "interaction_mode": payload.get("interaction_mode"),
                            "collected_at": collected_at,
                        },
                    }
                )
                if evidence_verified:
                    item["creation_evidence"] = evidence
                    item["can_use"] = True
                else:
                    item["can_use"] = False
                    item["unavailable_reason"] = "evidence_rejected"
            elif item.get("source_kind") == "report_url" and source_id in attempted_source_ids:
                item["can_use"] = False
                item["refresh_required"] = True
                item["freshness_class"] = "unverified"
                item["evidence_status"] = "failed"
                item["unavailable_reason"] = "refresh_failed"
            merged.append(item)
        return merged

    async def _validate_scrape_evidence(
        self,
        client: httpx.AsyncClient,
        scrape_payload: dict,
        *,
        require_metric: bool = False,
    ) -> dict:
        validation = self._compare_scrape_programmatic_channels(scrape_payload)
        if require_metric:
            metric_claims = [
                claim
                for claim in validation.get("verified_claims", [])
                if isinstance(claim, dict)
                and claim.get("claim_type") == "metric"
                and str(claim.get("value") or "").strip()
            ]
            validation["verified_claims"] = metric_claims
            if not metric_claims:
                validation["reason"] = "no_verified_metric"

        evidence = scrape_payload.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("id"):
            return {
                "validation_status": (
                    "verified" if validation.get("verified_claims") else "rejected"
                ),
                "evidence_kind": "structured_page",
                "validation": validation,
            }

        metadata_ok = (
            str(evidence.get("source_url") or "")
            == str(scrape_payload.get("url") or "")
            and str(evidence.get("page_title") or "")
            == str(scrape_payload.get("title") or "")
            and int(evidence.get("captured_at") or 0)
            == int(scrape_payload.get("collected_at") or -1)
        )
        validation["metadata_match"] = metadata_ok
        if not metadata_ok:
            validation["reason"] = "metadata_mismatch"
            validation["verified_claims"] = []

        image_url = str(evidence.get("image_url") or "")
        image_response = await client.get(f"{self.core_engine_base_url}{image_url}")
        if not image_response.is_success:
            validation["screenshot_status"] = "unreadable"
            return await self._persist_evidence_validation(
                client,
                evidence,
                "verified" if validation.get("verified_claims") else "rejected",
                validation,
            )

        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
                handle.write(image_response.content)
                temp_path = handle.name
            if self._ocr_engine is None:
                from ocr.engine import OcrEngine

                self._ocr_engine = OcrEngine.create_default()
            output = await asyncio.to_thread(self._ocr_engine.process, temp_path)
            ocr_validation = self._compare_scrape_with_ocr(scrape_payload, output.text)
            validation["ocr_confidence"] = round(float(output.confidence), 4)
            validation["ocr_verified_claim_count"] = len(
                ocr_validation.get("verified_claims", [])
            )
            validation["screenshot_status"] = (
                "matched"
                if ocr_validation.get("verified_claims")
                else "retained_unmatched"
            )
        except Exception as exc:
            logger.warning("创作证据 OCR 失败: %s", exc)
            validation["screenshot_status"] = "ocr_failed"
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        status = "verified" if validation.get("verified_claims") else "rejected"
        return await self._persist_evidence_validation(client, evidence, status, validation)

    @classmethod
    def _compare_scrape_programmatic_channels(cls, scrape_payload: dict) -> dict:
        """AX 为首选事实文本，DOM 为结构化后备；截图不参与可用性门禁。"""
        structured = scrape_payload.get("structured_data") or {}
        extraction = structured.get("extraction", {}) if isinstance(structured, dict) else {}
        primary = (
            str(extraction.get("primary") or "dom")
            if isinstance(extraction, dict)
            else "dom"
        )
        dom_text = (
            str(structured.get("dom_content_text") or "")
            if isinstance(structured, dict)
            else ""
        )
        dom_payload = dict(scrape_payload)
        dom_payload["content_text"] = dom_text or str(
            scrape_payload.get("content_text") or ""
        )
        candidates = cls._scrape_claim_candidates(dom_payload)
        if primary == "accessibility":
            verified = cls._match_claims_in_text(
                candidates,
                str(scrape_payload.get("content_text") or ""),
            )
            return {
                "reason": "ax_dom_matched" if verified else "ax_dom_mismatch",
                "primary_channel": "accessibility",
                "secondary_channel": "dom",
                "verified_claims": verified,
            }
        return {
            "reason": "dom_structured" if candidates else "dom_empty",
            "primary_channel": "dom",
            "secondary_channel": None,
            "verified_claims": candidates[:20],
        }

    @classmethod
    def _match_claims_in_text(cls, claims: list[dict], target_text: str) -> list[dict]:
        normalized_target = cls._normalize_evidence_text(target_text)
        verified: list[dict] = []
        for claim in claims:
            if claim.get("claim_type") == "text":
                statement = str(claim.get("statement") or "")
                normalized_statement = cls._normalize_evidence_text(statement)
                tokens = cls._evidence_match_tokens(statement)
                matched_tokens = [
                    token
                    for token in tokens
                    if cls._normalize_evidence_text(token) in normalized_target
                ]
                if (
                    normalized_statement
                    and normalized_statement in normalized_target
                ) or (
                    len(matched_tokens) >= 2
                    and len(matched_tokens) / max(1, len(tokens)) >= 0.6
                ):
                    verified.append(claim)
            else:
                value = cls._normalize_evidence_text(str(claim.get("value") or ""))
                labels = cls._evidence_match_tokens(str(claim.get("label") or ""))
                if value and value in normalized_target and any(
                    cls._normalize_evidence_text(token) in normalized_target
                    for token in labels
                ):
                    verified.append(claim)
            if len(verified) >= 20:
                break
        return verified

    async def _persist_evidence_validation(
        self,
        client: httpx.AsyncClient,
        evidence: dict,
        status: str,
        validation: dict,
    ) -> dict:
        try:
            response = await client.post(
                f"{self.core_engine_base_url}/api/creation/evidence/{evidence['id']}/validate",
                json={"status": status, "validation": validation},
            )
            if response.is_success:
                return response.json()
        except Exception as exc:
            logger.warning("保存创作证据校验状态失败: %s", exc)
        return {**evidence, "validation_status": status, "validation": validation}

    @classmethod
    def _compare_scrape_with_ocr(cls, scrape_payload: dict, ocr_text: str) -> dict:
        evidence = scrape_payload.get("evidence") or {}
        metadata_ok = (
            str(evidence.get("source_url") or "") == str(scrape_payload.get("url") or "")
            and str(evidence.get("page_title") or "") == str(scrape_payload.get("title") or "")
            and int(evidence.get("captured_at") or 0) == int(scrape_payload.get("collected_at") or -1)
        )
        if not metadata_ok:
            return {
                "reason": "metadata_mismatch",
                "metadata_match": False,
                "verified_claims": [],
            }

        normalized_ocr = cls._normalize_evidence_text(ocr_text)
        verified_claims: list[dict] = []
        for claim in cls._scrape_claim_candidates(scrape_payload):
            if claim.get("claim_type") == "text":
                statement = str(claim.get("statement") or "")
                normalized_statement = cls._normalize_evidence_text(statement)
                tokens = cls._evidence_match_tokens(statement)
                matched_tokens = [
                    token
                    for token in tokens
                    if cls._normalize_evidence_text(token) in normalized_ocr
                ]
                text_match = (
                    bool(normalized_statement)
                    and normalized_statement in normalized_ocr
                ) or (
                    len(matched_tokens) >= 2
                    and len(matched_tokens) / max(1, len(tokens)) >= 0.6
                )
                if text_match:
                    verified_claims.append(claim)
                if len(verified_claims) >= 20:
                    break
                continue
            value = cls._normalize_evidence_text(str(claim.get("value") or ""))
            labels = cls._evidence_match_tokens(str(claim.get("label") or ""))
            value_match = bool(value) and value in normalized_ocr
            label_match = any(
                cls._normalize_evidence_text(token) in normalized_ocr for token in labels
            )
            if value_match and label_match:
                verified_claims.append(claim)
            if len(verified_claims) >= 20:
                break
        return {
            "reason": "matched" if verified_claims else "dom_ocr_mismatch",
            "metadata_match": True,
            "verified_claims": verified_claims,
        }

    @classmethod
    def _scrape_claim_candidates(cls, scrape_payload: dict) -> list[dict]:
        structured = scrape_payload.get("structured_data") or {}
        candidates: list[dict] = []
        tables = structured.get("tables", []) if isinstance(structured, dict) else []
        if (
            isinstance(tables, list)
            and tables
            and all(isinstance(row, list) for row in tables)
            and all(not isinstance(cell, list) for cell in tables[0])
        ):
            tables = [tables]
        for table in tables:
            if not isinstance(table, list):
                continue
            for row in table:
                if not isinstance(row, list):
                    continue
                cells = [str(cell).strip() for cell in row if str(cell).strip()]
                row_text = " ".join(cells)
                if cls._is_scrape_noise(row_text):
                    continue
                values = re.findall(r"(?<!\w)[+-]?\d[\d,]*(?:\.\d+)?%?", row_text)
                label = " ".join(cell for cell in cells if not re.fullmatch(r"[+-]?[\d,.]+%?", cell))
                for value in values:
                    if cls._is_unhelpful_metric_value(value):
                        continue
                    candidates.append(
                        {
                            "claim_type": "metric",
                            "label": label[:240],
                            "value": value,
                            "statement": row_text[:500],
                        }
                    )
        labels = structured.get("metric_labels", []) if isinstance(structured, dict) else []
        for item in labels if isinstance(labels, list) else []:
            text = str(item).strip()
            if cls._is_scrape_noise(text):
                continue
            for value in re.findall(r"(?<!\w)[+-]?\d[\d,]*(?:\.\d+)?%?", text):
                if cls._is_unhelpful_metric_value(value):
                    continue
                label = text.replace(value, " ").strip(" :-—")
                candidates.append(
                    {
                        "claim_type": "metric",
                        "label": label[:240],
                        "value": value,
                        "statement": text[:500],
                    }
                )
        text_blocks = structured.get("text_blocks", []) if isinstance(structured, dict) else []
        for item in text_blocks if isinstance(text_blocks, list) else []:
            text = " ".join(str(item).split()).strip()
            if (
                not 8 <= len(text) <= 240
                or re.search(r"\d", text)
                or cls._is_scrape_noise(text)
            ):
                continue
            tokens = cls._evidence_match_tokens(text)
            if len(tokens) < 2:
                continue
            candidates.append(
                {
                    "claim_type": "text",
                    "label": " ".join(tokens[:4])[:120],
                    "value": "",
                    "statement": text,
                }
            )

        # BI 指标卡经常不是 table/aria-label，而是“指标名 → 日期 → 数值”的
        # 相邻 DOM 文本。无论页面里是否还有缓存设置表，都必须额外解析正文，
        # 否则真正业务指标会被界面噪声遮蔽。
        content_lines = [
            " ".join(line.split()).strip()
            for line in str(scrape_payload.get("content_text") or "").splitlines()[:800]
            if " ".join(line.split()).strip()
        ]
        statistical_period = cls._content_statistical_period(content_lines)
        date_pattern = re.compile(r"^20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?$")
        value_pattern = re.compile(
            r"^[+-]?\d[\d,]*(?:\.\d+)?(?:%|亿元|万元|亿|万|千|百|卡|个|次|元|秒|ms|s|x)?$",
            re.IGNORECASE,
        )
        for index, line in enumerate(content_lines):
            if not value_pattern.fullmatch(line) or date_pattern.fullmatch(line):
                continue
            if cls._is_unhelpful_metric_value(line):
                continue
            label = ""
            nearest_date = ""
            for previous in reversed(content_lines[max(0, index - 4) : index]):
                if date_pattern.fullmatch(previous):
                    nearest_date = nearest_date or previous
                    continue
                if value_pattern.fullmatch(previous) or previous in {"至", "到", "-", "—"}:
                    continue
                if cls._is_scrape_noise(previous):
                    continue
                label = previous
                break
            if not label:
                continue
            period = statistical_period or nearest_date
            statement = " ".join(part for part in (label, period, line) if part)
            candidates.append(
                {
                    "claim_type": "metric",
                    "label": label[:240],
                    "value": line,
                    "statement": statement[:500],
                    "statistical_period": period,
                }
            )

        deduplicated: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            key = (
                cls._normalize_evidence_text(str(candidate.get("label") or "")),
                cls._normalize_evidence_text(str(candidate.get("value") or "")),
                str(candidate.get("claim_type") or "metric"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(candidate)
        return deduplicated[:500]

    @staticmethod
    def _content_statistical_period(lines: list[str]) -> str:
        date_pattern = re.compile(r"^20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?$")
        for index in range(len(lines) - 2):
            if (
                date_pattern.fullmatch(lines[index])
                and lines[index + 1] in {"至", "到", "-", "—"}
                and date_pattern.fullmatch(lines[index + 2])
            ):
                return f"{lines[index]} 至 {lines[index + 2]}"
        return ""

    @staticmethod
    def _is_unhelpful_metric_value(value: str) -> bool:
        normalized = value.strip().replace(",", "").lower()
        return normalized in {"0", "0.0", "0%", "0.0%"}

    @staticmethod
    def _is_scrape_noise(value: str) -> bool:
        normalized = " ".join(value.lower().split())
        return any(
            marker in normalized
            for marker in (
                "加载中",
                "loading",
                "缓存命中",
                "开启缓存",
                "未开启缓存",
                "未发起查询",
                "当前看板内无图表",
                "开始创建图表",
                "图表名称",
                "筛选器关联",
                "权限设置",
                "告警设置",
                "异步查询列表",
            )
        )

    @staticmethod
    def _normalize_evidence_text(value: str) -> str:
        return re.sub(r"[\s,，:：;；|｜]", "", value).lower()

    @staticmethod
    def _evidence_match_tokens(value: str) -> list[str]:
        tokens: list[str] = []
        for english in re.findall(r"[a-zA-Z]{2,}", value):
            lowered = english.lower()
            if lowered not in tokens:
                tokens.append(lowered)
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", value):
            chars = list(sequence)
            for index in range(max(1, len(chars) - 1)):
                token = "".join(chars[index : index + 2])
                if len(token) == 2 and token not in tokens:
                    tokens.append(token)
        return tokens[:24]

    async def generate_document(
        self,
        user_prompt: str,
        design_templates: list[dict],
        timeline_context: Optional[str] = None,
        capture_context: Optional[str] = None,
        options: Optional[CreationOptions] = None,
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """流式生成文档。"""
        options = options or CreationOptions()
        parsed = self.analyze_requirement(user_prompt, options)
        references = self.retrieve_references(user_prompt, parsed, options) if options.enable_rag else []
        web_results = (
            await self.collect_web_context(user_prompt, parsed)
            if options.enable_web_search or parsed.get("needs_latest")
            else []
        )

        system_prompt = self._build_system_prompt(design_templates, options)
        user_message = self._build_user_message(
            user_prompt=user_prompt,
            timeline_context=timeline_context,
            capture_context=capture_context,
            options=options,
            parsed_requirement=parsed,
            references=references,
            web_results=web_results,
        )

        local_model = creation_model or self.model
        logger.info("使用模型: %s", local_model)
        logger.info("创作类型: %s, 参考资料: %s", parsed.get("doc_type") or "未指定", len(references))

        if creation_model and creation_api_key:
            output_parts: list[str] = []
            started_ms = int(time.time() * 1000)
            try:
                async for chunk in self._generate_cloud(system_prompt, user_message, creation_model, creation_api_key, creation_base_url or ""):
                    output_parts.append(chunk)
                    yield chunk
                self._log_creation_usage(
                    model_name=creation_model,
                    prompt_text=system_prompt + "\n\n" + user_message,
                    response_text="".join(output_parts),
                    latency_ms=int(time.time() * 1000) - started_ms,
                    status="success",
                )
            except Exception as exc:
                self._log_creation_usage(
                    model_name=creation_model,
                    prompt_text=system_prompt + "\n\n" + user_message,
                    response_text="".join(output_parts),
                    latency_ms=int(time.time() * 1000) - started_ms,
                    status="failed",
                    error_msg=str(exc),
                )
                raise
            return

        # Qwen3.5 在 Ollama chat 模式下有 thinking 解析 bug，导致长时间不输出内容。
        # 改用 /api/generate raw 模式，绕过有问题的 chat 解析器。
        is_qwen35 = "qwen3.5" in local_model.lower()

        if is_qwen35:
            prompt = self._build_qwen35_prompt(system_prompt, user_message)
            payload = {
                "model": local_model,
                "prompt": prompt,
                "raw": True,
                "stream": True,
                "options": {
                    "temperature": 0.65,
                    "top_p": 0.9,
                    "num_predict": 4096,
                },
            }
            endpoint = "/api/generate"
        else:
            payload = {
                "model": local_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": True,
                "options": {
                    "temperature": 0.65,
                    "top_p": 0.9,
                    "num_predict": 4096,
                },
            }
            endpoint = "/api/chat"

        chunk_count = 0
        output_parts: list[str] = []
        started_ms = int(time.time() * 1000)
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST", f"{self.ollama_base_url}{endpoint}", json=payload
                ) as response:
                    response.raise_for_status()
                    if is_qwen35:
                        async for chunk in self._stream_qwen35_raw(response):
                            if chunk:
                                output_parts.append(chunk)
                                chunk_count += 1
                                if chunk_count % 100 == 0:
                                    logger.info("已生成 %s 个块", chunk_count)
                                yield chunk
                    else:
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if content:
                                output_parts.append(content)
                                chunk_count += 1
                                if chunk_count % 100 == 0:
                                    logger.info("已生成 %s 个块", chunk_count)
                                yield content

            logger.info("生成完成，总共 %s 个块", chunk_count)
            self._log_creation_usage(
                model_name=local_model,
                prompt_text=system_prompt + "\n\n" + user_message,
                response_text="".join(output_parts),
                latency_ms=int(time.time() * 1000) - started_ms,
                status="success",
            )
        except Exception as exc:
            self._log_creation_usage(
                model_name=local_model,
                prompt_text=system_prompt + "\n\n" + user_message,
                response_text="".join(output_parts),
                latency_ms=int(time.time() * 1000) - started_ms,
                status="failed",
                error_msg=str(exc),
            )
            raise

    def build_routing_prompts(
        self,
        query: str,
        requirement: dict,
        selected_skills: Iterable[dict] = (),
    ) -> tuple[str, str]:
        """系统提示词动态加载各能力的自描述（渐进式披露），由模型决策执行链路。

        路由倾向不在这里硬编码：每个 Tool / Agent（Agent as Tool）/ Skill 在自身
        定义处声明解决什么问题、在什么目标下使用，这里只负责加载。
        """
        skill_lines = self._skill_description_lines(selected_skills)
        capability_lines = routing_capability_lines(skill_lines)
        system = (
            "你是创作 Agent 的执行链路路由决策器。下面是每个能力自己声明的描述，"
            "请依据这些描述为完成用户请求选择需要的能力，只做选择，不写正文。\n\n"
            "可选能力（描述由各能力自行声明）：\n"
            + "\n".join(capability_lines)
            + "\n\n决策原则：\n"
            "1. 依据每个能力的自描述选择能力，与请求无关的能力不要加\n"
            "2. memory_search 和网页刷新是结构性能力，不需要你决策\n"
            "3. 只输出一个 JSON 对象，不要输出任何其他内容\n\n"
            '输出格式：{"tools": [...], "agents": [...], '
            '"reasoning": "不超过 50 字的理由"}'
        )
        topic = str(requirement.get("topic") or "").strip()
        doc_type = str(requirement.get("doc_type") or "").strip()
        audience = str(requirement.get("audience") or "").strip()
        context_lines = [f"用户请求：{query.strip()}"]
        if doc_type:
            context_lines.append(f"文档类型：{doc_type}")
        if topic and topic != query.strip():
            context_lines.append(f"主题：{topic}")
        if audience:
            context_lines.append(f"目标读者：{audience}")
        if requirement.get("needs_latest"):
            context_lines.append("附加信号：需求解析认为涉及最新外部信息")
        return system, "\n".join(context_lines)

    @staticmethod
    def _skill_description_lines(
        selected_skills: Iterable[dict],
    ) -> list[str]:
        """Skill 同样以自己的描述参与披露；Skill 自动应用，不进入决策输出。"""
        lines: list[str] = []
        for item in selected_skills or ():
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            description = (
                item.get("skillDescription")
                or item.get("skill_description")
                or {}
            )
            purpose = ""
            problems = ""
            if isinstance(description, dict):
                purpose = str(description.get("purpose") or "").strip()
                raw_problems = description.get("problems") or []
                if isinstance(raw_problems, (list, tuple)):
                    problems = "；".join(str(part) for part in raw_problems if part)
            text = purpose or str(item.get("summary") or "").strip()
            if problems:
                text = f"{text}（解决的问题：{problems}）" if text else problems
            if not title or not text:
                continue
            lines.append(
                f"- {title} (Skill 上下文): {text} 该 Skill 会自动应用，无需写入决策输出。"
            )
        return lines

    def parse_routing_decision(self, text: str) -> dict:
        """只做输出校验：提取 JSON，解析失败时抛出 ValueError。"""
        candidate = (text or "").strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate).strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("路由决策输出中没有 JSON 对象")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"路由决策输出不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("路由决策输出必须是 JSON 对象")
        decision = validate_routing_decision(parsed)
        decision["reasoning"] = str(parsed.get("reasoning") or "")[:200]
        return decision

    async def route_capabilities(
        self,
        *,
        query: str,
        requirement: dict,
        selected_skills: Iterable[dict] = (),
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
    ) -> dict:
        """由模型推理路由决策；失败时降级为保守回退，不阻断创作链路。"""
        system_prompt, user_prompt = self.build_routing_prompts(
            query, requirement, selected_skills
        )
        started_ms = int(time.time() * 1000)
        model_name = creation_model or self.model
        parts: list[str] = []
        try:
            async for chunk in self._stream_direct_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                creation_model=creation_model,
                creation_api_key=creation_api_key,
                creation_base_url=creation_base_url,
                num_predict=400,
                temperature=0.1,
            ):
                parts.append(chunk)
            response_text = "".join(parts)
            decision = self.parse_routing_decision(response_text)
            decision["source"] = "model"
            self._log_creation_usage(
                model_name=model_name,
                prompt_text=system_prompt + "\n\n" + user_prompt,
                response_text=response_text,
                latency_ms=int(time.time() * 1000) - started_ms,
                status="success",
            )
            return decision
        except Exception as exc:
            logger.warning("路由模型推理失败，降级为保守路由: %s", exc)
            self._log_creation_usage(
                model_name=model_name,
                prompt_text=system_prompt + "\n\n" + user_prompt,
                response_text="".join(parts),
                latency_ms=int(time.time() * 1000) - started_ms,
                status="failed",
                error_msg=str(exc),
            )
            return fallback_routing_decision(query, requirement)

    async def run_specialist_agent(
        self,
        *,
        agent_id: str,
        system_prompt: str,
        user_prompt: str,
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
    ) -> str:
        """执行一个需要模型推理的专业子 Agent，并返回可写回环境的结论。"""
        parts: list[str] = []
        started_ms = int(time.time() * 1000)
        model_name = creation_model or self.model
        try:
            async for chunk in self._stream_direct_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                creation_model=creation_model,
                creation_api_key=creation_api_key,
                creation_base_url=creation_base_url,
                num_predict=1600,
                temperature=0.25,
            ):
                parts.append(chunk)
            result = "".join(parts).strip()
            if not result:
                raise RuntimeError(f"{agent_id} 未返回分析结果")
            self._log_creation_usage(
                model_name=model_name,
                prompt_text=system_prompt + "\n\n" + user_prompt,
                response_text=result,
                latency_ms=int(time.time() * 1000) - started_ms,
                status="success",
            )
            return result
        except Exception as exc:
            self._log_creation_usage(
                model_name=model_name,
                prompt_text=system_prompt + "\n\n" + user_prompt,
                response_text="".join(parts),
                latency_ms=int(time.time() * 1000) - started_ms,
                status="failed",
                error_msg=str(exc),
            )
            raise

    async def stream_agent_document(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        creation_model: Optional[str] = None,
        creation_api_key: Optional[str] = None,
        creation_base_url: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """执行文档撰写 Agent，并把最终文档按块返回。"""
        async for chunk in self._stream_direct_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            creation_model=creation_model,
            creation_api_key=creation_api_key,
            creation_base_url=creation_base_url,
            num_predict=8192,
            temperature=0.55,
        ):
            yield chunk

    async def _stream_direct_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        creation_model: Optional[str],
        creation_api_key: Optional[str],
        creation_base_url: Optional[str],
        num_predict: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """统一子 Agent 的本地/自带密钥模型调用，不包含 RAG 等上层编排。"""
        if creation_model and creation_api_key:
            async for chunk in self._generate_cloud(
                system_prompt,
                user_prompt,
                creation_model,
                creation_api_key,
                creation_base_url or "",
            ):
                yield chunk
            return

        local_model = creation_model or self.model
        is_qwen35 = "qwen3.5" in local_model.lower()
        if is_qwen35:
            payload = {
                "model": local_model,
                "prompt": self._build_qwen35_prompt(system_prompt, user_prompt),
                "raw": True,
                "stream": True,
                "options": {
                    "temperature": temperature,
                    "top_p": 0.9,
                    "num_predict": num_predict,
                },
            }
            endpoint = "/api/generate"
        else:
            payload = {
                "model": local_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": True,
                "options": {
                    "temperature": temperature,
                    "top_p": 0.9,
                    "num_predict": num_predict,
                },
            }
            endpoint = "/api/chat"

        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST", f"{self.ollama_base_url}{endpoint}", json=payload
            ) as response:
                response.raise_for_status()
                if is_qwen35:
                    async for chunk in self._stream_qwen35_raw(response):
                        if chunk:
                            yield chunk
                else:
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content

    async def analyze_creation_skill(
        self,
        document_title: str,
        document_content: str,
        doc_type: str = "",
    ) -> dict:
        """用本地模型提炼文档创作方式；模型不可用时返回可编辑的本地规则分析。"""
        title = document_title.strip()
        content = document_content.strip()
        if not title or len(title) > 200:
            raise ValueError("文档标题需要在 1 到 200 个字符之间")
        if len(content) < 20 or len(content) > 80000:
            raise ValueError("文档内容需要在 20 到 80000 个字符之间")

        style_content = self._select_creation_skill_style_content(title, content)
        prompt = self._build_creation_skill_analysis_prompt(title, style_content, doc_type)
        payload = self._creation_skill_analysis_payload(self.model, prompt)
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(f"{self.ollama_base_url}/api/generate", json=payload)
                response.raise_for_status()
                raw = response.json().get("response", "")
            parsed = self._normalize_creation_skill_analysis(
                json.loads(raw), title, style_content, doc_type
            )
            parsed["analysis_mode"] = "local_model"
            return parsed
        except Exception as exc:
            logger.warning("本地模型提炼技能失败，使用规则分析: %s", exc)
            fallback = self._fallback_creation_skill_analysis(
                title, style_content, doc_type
            )
            fallback["analysis_mode"] = "heuristic_fallback"
            if isinstance(exc, json.JSONDecodeError):
                fallback["fallback_reason"] = "invalid_model_output"
            elif isinstance(exc, httpx.TimeoutException):
                fallback["fallback_reason"] = "model_timeout"
            elif isinstance(exc, httpx.HTTPError):
                fallback["fallback_reason"] = "model_request_failed"
            else:
                fallback["fallback_reason"] = "analysis_failed"
            return fallback

    @staticmethod
    def _select_creation_skill_style_content(
        document_title: str, document_content: str, maximum: int = 30000
    ) -> str:
        """只取与主文档连续相关的 Markdown 主章节，隔离 Bake 追加的异质正文。"""
        content = str(document_content or "").strip()
        if not content:
            return ""
        matches = list(re.finditer(r"(?m)^\s{0,3}#\s+(.+?)\s*$", content))
        if not matches:
            return content[:maximum]

        normalized_title = re.sub(
            r"[\s\W_]+", "", document_title, flags=re.UNICODE
        ).lower()
        title_core = re.sub(
            r"(?:整体)?(?:技术)?(?:方案|文档|报告|设计|规划|说明|手册|指南)$",
            "",
            normalized_title,
        )
        ascii_anchors = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", document_title)
            if token.lower() not in {"the", "and", "for", "with"}
        }

        start_index = 0
        for index, match in enumerate(matches):
            heading = re.sub(r"[\s\W_]+", "", match.group(1), flags=re.UNICODE).lower()
            if heading == normalized_title:
                start_index = index
                break

        selected: list[str] = []
        for index in range(start_index, len(matches)):
            match = matches[index]
            heading = match.group(1).strip()
            normalized_heading = re.sub(
                r"[\s\W_]+", "", heading, flags=re.UNICODE
            ).lower()
            heading_tokens = {
                token.lower()
                for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", heading)
            }
            related = (
                index == start_index
                or bool(ascii_anchors & heading_tokens)
                or (
                    len(title_core) >= 3
                    and (
                        title_core in normalized_heading
                        or normalized_heading in title_core
                    )
                )
            )
            looks_like_appendix = bool(
                re.search(
                    r"近期|补充|更新版|最新调研|浏览(?:记录|快照)|页面快照|历史版本|专项资源|用户行为|^\s*20\d{2}[年/-]",
                    heading,
                )
            )
            if not related and looks_like_appendix:
                break
            block_end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(content)
            )
            selected.append(content[match.start():block_end].strip())

        excerpt = "\n\n".join(selected).strip()
        return (excerpt or content[matches[start_index].start():])[:maximum]

    @staticmethod
    def _creation_skill_analysis_payload(model: str, prompt: str) -> dict:
        return {
            "model": model,
            "prompt": prompt,
            "stream": False,
            # 普通 JSON 模式下，长结果仍可能包含未闭合字符串或缺失分隔符。
            # 传入完整 Schema，让 Ollama 在解码阶段约束输出结构。
            "format": CREATION_SKILL_ANALYSIS_SCHEMA,
            # Qwen 3.5 默认把 JSON 写入 thinking，response 为空；关闭思考后
            # Ollama 才会把可解析的结构化结果放入 response。
            "think": False,
            # 完整示例文档本身需要保留足够篇幅，再加上五类风格指纹，
            # 四千 token 很容易让 JSON 在 example_document 中途被截断。
            "options": {"temperature": 0.2, "top_p": 0.8, "num_predict": 8192},
        }

    @staticmethod
    def _build_creation_skill_analysis_prompt(title: str, content: str, doc_type: str) -> str:
        return f"""你是 MemoryBread 的本地文档风格分析器。你的任务不是给出通用写作建议，而是为这份源文档制作一枚可复用的“风格指纹”：后续模型只看分析结果，也能明显模仿作者的标题句式、行文组织、惯用话术和配图方式。

文档标题：{title}
文档类型：{doc_type or '未指定'}
文档正文：
{content}

Skill 命名与简介原则：
- 标题要高度概括可复用的工作场景和交付目标，而不是复述源文档标题。
- 标题中禁止出现具体公司、部门、事业部、团队、项目、产品、客户或人员名称。
- 标题优先使用“适用场景 + 文档/方案/报告”的形式，例如源文档来自某研发部门的技术沟通会时，写成“跨部门技术沟通会文档”。
- 简介必须说明这个 Skill 适合在什么场景、帮助谁完成什么目标，不能只罗列写作风格。

来源忠实度原则（适用于 JSON 中的每一个字段）：
- 每条结论都必须能在源文档中找到依据。不要只写“标题突出重点”“表达专业正式”“结论先行”“图文结合”这类放到任何文档都成立的判断；如果使用，必须继续说清作者具体在哪一层、按什么顺序、用什么句式或词语实现。
- 可以保留源文档的非敏感短语、专业用语、虚词、动词、句式、标点和标题骨架；禁止复制完整句子或连续正文。
- 标题示例要以源文档子标题为直接母版，只替换其中可能泄密的主语、宾语、专名、数字和事实，尽量保持原有词序、动词、虚词、标点、长短和语气，不能另起炉灶写成通用标题。
- example_document 使用全新虚构主题，但必须实际复现本次提炼出的标题句式、章节推进方式、过渡话术、专业表达和图示习惯。
- 标题设计风格不能只给形容词。每条都按“可观察特征 → 可复用句式骨架 → 适用位置或使用边界”写，让创作者可以照着执行；至少覆盖层级分工、长度节奏、词序句法、标点和常用动作词。
- 行文设计思路不能只列章节名称。要依次说清开篇如何定调、章节如何递进、段内先写判断还是依据、列表何时出现、信息密度如何控制、结尾如何收束，并明确哪些写法不应迁移。
- 固定字段之外，还要寻找真正属于这份源文档的高辨识度写法，例如定义先行、短标签加冒号、代码与解释成对出现、分隔线切换议题、括号注脚或双入口并列。只在有明确证据时放入 distinctive_sections，输出零至四个，可提炼多个，但不能换名重复固定字段。
- skill_description 是给创作 Agent 做触发判断的能力说明，不是宣传文案。必须明确能创作哪些文档、解决哪些问题、涉及哪些领域以及交付什么。
- execution_steps 描述如何从需求走到成稿。按真实先后顺序给出三至八步，并为每一步明确目标、产出，以及可调用的 Agent、Skill、Tool；没有必要的资源数组留空，不得为了显得智能而堆满能力。
- 每一步的 Agent 与 Tool 合计最多四个；只列这一步真正需要的能力。同一能力可以在不同步骤重复出现，例如先调研、后复核。
- Agent 只能从 industry_research_agent、data_analysis_agent、solution_design_agent、document_writer_agent、quality_review_agent 中选择；Tool 只能从 memory_search、internet_search、data_search、webpage_scrape、github_search、plantuml_diagram 中选择。Skill 使用可复用技能名称或稳定标识，没有依赖时留空。

隐私与通用化原则：
- 禁止出现真实或可推断的公司、事业群、事业部、部门、团队、项目、产品、系统、客户、人员、地域、日期、指标和金额。
- 把敏感主语或宾语最小替换为“目标对象”“相关角色”“示例项目”“通用服务”等抽象词；替换后继续保留原有表达骨架，不要把整句重写成另一种风格。
- 不输出源文档事实、论点或完整句子。允许收集可安全复用的短话术和专业词语，并说明作者如何使用它们。
- 所有字段禁止出现阿拉伯数字；需要表达顺序时使用 Markdown 无序列表，需要表达大致长度时使用中文数词。
- 脱敏后的标题必须仍是自然、可理解的中文标题。“目标对象 目标对象”“相关角色相关角色”或只剩占位词的结果属于错误，必须重写为保留原句式的完整标题。

完整示例文档质量标准：
- 正文目标篇幅为一千二百至两千二百个中文字符，不含 Markdown 标记；不能用摘要式短文冒充完整文档。
- 至少包含一个主标题、摘要、六个承担不同内容角色的二级章节和结论。每个核心章节至少有两个完整段落，必要时加入同语法起点的无序列表。
- 开篇、章节过渡、段内展开、列表和收束都要实际使用本次提炼出的写法；不能只是章节名相似。
- 若 diagram_style 明确默认不生成图片，示例中也不强行加图；若源文档有稳定图示习惯，则提供一段可执行的 PlantUML 或 Mermaid 代码，并在正文中先解释图的阅读方式。
- 输出前检查：文档主题必须完全虚构，标题示例语义完整，JSON 字符串正确转义，所有字段均无阿拉伯数字。

JSON 类型硬约束：
- common_titles、writing_guidelines、suggested_category_keywords 只能是字符串数组；数组项禁止使用对象、键值对或嵌套数组。
- field_examples 的每个值只能是字符串数组。
- 只有 distinctive_sections 是对象数组。不要把 Python 字典、JSON 对象或类似 {{'level': '一级标题'}} 的文本塞进字符串数组。

类目候选（必须从以下有效路径中选择最接近的一整条，不得自行创造名称或拼接路径）：
- 互联网 / 电商零售 / 产品经理 / 产品设计文档
- 互联网 / 电商零售 / 产品经理 / 产品需求文档
- 互联网 / 电商零售 / UI/UX 设计师 / UI 设计文档
- 互联网 / 电商零售 / UI/UX 设计师 / 用户体验设计文档
- 互联网 / 电商零售 / 软件工程师 / 技术设计文档
- 互联网 / 电商零售 / 软件工程师 / 接口设计文档
- 互联网 / 电商零售 / 架构师 / 技术架构设计文档
- 互联网 / 电商零售 / 运营 / 运营方案
- 互联网 / 企业服务 / 产品经理 / 产品设计文档
- 互联网 / 企业服务 / 软件工程师 / 技术设计文档
- 互联网 / 企业服务 / 架构师 / 技术架构设计文档
- 互联网 / 企业服务 / 客户成功顾问 / 客户实施方案
- 金融 / 银行与支付 / 风控经理 / 风险策略文档
- 金融 / 银行与支付 / 数据分析师 / 数据分析报告
- 金融 / 银行与支付 / 产品经理 / 金融产品设计文档
- 金融 / 银行与支付 / 架构师 / 技术架构设计文档
- 金融 / 保险 / 产品经理 / 保险产品设计文档
- 金融 / 保险 / 精算与风险 / 精算分析报告
- 金融 / 保险 / 理赔运营 / 理赔处理 SOP
- 制造 / 智能制造 / 工艺工程师 / 工艺设计文档
- 制造 / 智能制造 / 软件工程师 / 工业软件设计文档
- 制造 / 智能制造 / 架构师 / 智能制造架构文档
- 制造 / 消费品制造 / 工业设计师 / 工业设计文档
- 制造 / 消费品制造 / 质量工程师 / 质量策划文档
- 专业服务 / 咨询与研究 / 咨询顾问 / 项目建议书
- 专业服务 / 咨询与研究 / 咨询顾问 / 咨询方案报告
- 专业服务 / 咨询与研究 / 研究分析师 / 行业研究报告
- 专业服务 / 咨询与研究 / 项目经理 / 项目管理计划
- 专业服务 / 品牌与内容 / 品牌策划 / 品牌策略方案
- 专业服务 / 品牌与内容 / 内容运营 / 内容策划文档
- 专业服务 / 品牌与内容 / 视觉设计师 / 视觉设计规范

只输出一个 JSON 对象，字段必须完整：
{{
  "title": "高度概括适用场景和交付目标的短标题，不超过四十字，不含任何具体组织或项目名称",
  "summary": "说明适用场景、适用对象和创作目标，不超过一百六十字",
  "skill_description": {{
    "purpose": "说明这个 Skill 在什么场景下，为谁解决什么创作问题",
    "document_types": ["可以创作的具体文档类型"],
    "problems": ["要解决的内容组织、分析、决策或交付问题"],
    "domains": ["涉及的行业、职能或专业领域；无明确领域时可为空"],
    "deliverables": ["执行完成后应产出的可验收成果"]
  }},
  "execution_steps": [
    {{
      "id": "使用小写英文、数字和连字符组成的稳定步骤标识",
      "title": "步骤短标题",
      "objective": "这一步要完成什么，以及为什么要先完成它",
      "output": "这一步留给下一步的明确产出",
      "agents": ["从允许的 Agent 标识中选择"],
      "skills": ["需要协同调用的其它 Skill；没有则为空数组"],
      "tools": ["从允许的 Tool 标识中选择"]
    }}
  ],
  "common_titles": ["标题设计风格：写五至八条从源文档子标题归纳出的具体规则；每条包含可观察特征、可复用句式骨架、适用位置或边界，覆盖标题层级、词序、常用动词或名词、虚词、长度和标点"],
  "title_style": "旧版兼容字段：复制 common_titles 中的内容，不增加新的标题规则",
  "text_style": "行文设计思路：用四百至七百个中文字符写成可执行配方，依次说明开篇定调、章节递进、段内顺序、列表条件、信息密度、过渡方式、结尾收束和不应迁移的写法；每一项都落到源文档可观察到的组织特征",
  "diagram_style": "图片生成方式：用四百至七百个中文字符写成可执行配方。依次说明源文档是否存在图示证据、什么情况下才需要生成、推荐 PlantUML 或 Mermaid 的哪一种图、正文信息如何筛选、阅读方向与分组如何安排、节点与连线怎样命名、颜色与边界怎样克制、正文在图前后怎样引导，以及哪些内容禁止画入和交付前如何自检；没有图示依据时先明确写“默认不生成图片”，再说明真正需要补图的触发条件，不能为了完整感强行配图",
  "writing_guidelines": ["话术表达风格：输出五至八条、合计四百至七百个中文字符的可执行规则。每条从源文档中的非敏感原词、短语、过渡语、动作动词、专业用语、标点或句式出发，写清证据表达、常见位置、承担作用、句法语气、迁移方式和使用边界；没有稳定证据时明确禁止额外植入模板话术，不要改写成放到任何文档都成立的通用规范"],
  "distinctive_sections": [
    {{
      "title": "源文档独有写法的短名称，不与标题、行文、配图、话术四个固定章节重名",
      "description": "说明这种写法在源文档里如何出现、为什么形成辨识度",
      "guidance": "说明后续创作在什么位置、按什么步骤复刻，以及不适用的边界",
      "examples": ["一至三个使用全新虚构主题的完整示例，不能是占位词、残句或源文复述"]
    }}
  ],
  "section_headings": {{
    "common_titles": "标题设计风格",
    "title_style": "标题设计风格",
    "text_style": "行文设计思路",
    "diagram_style": "图片生成方式",
    "writing_guidelines": "话术表达风格"
  }},
  "field_examples": {{
    "common_titles": ["四至六个以源文档子标题为母版的脱敏仿写示例：只替换敏感主语、宾语、专名和事实，其余句式与标点尽量不动；示例必须语义完整且不能连续重复占位词"],
    "title_style": ["旧版兼容字段：复制 common_titles 示例"],
    "text_style": ["一至三个使用全新虚构主题、但严格复现源文档组织次序和段落推进的正文片段"],
    "diagram_style": ["一至三个可执行的代码生图说明，明确 PlantUML 或 Mermaid 的图类型、启用条件、信息范围、元素、布局、标注和图文衔接方式；必要时给出短代码骨架"],
    "writing_guidelines": ["一至三个把收集到的惯用短语、动词、标点或专业表达迁移到虚构主题中的完整仿写句，并让示例体现对应规则的出现位置和语气"]
  }},
  "example_document": "一份一千二百至两千二百个中文字符的完整 Markdown 示例文档，使用全新虚构主题，至少包含主标题、摘要、六个二级章节和结论；核心章节至少两个完整段落，必须实际体现上述标题句式、行文逻辑、话术和图示方式，不得出现源文档中的名称、事实、阿拉伯数字或完整句子",
  "suggested_category_keywords": ["从候选中选择的行业", "从候选中选择的细分行业", "从候选中选择的工种", "从候选中选择的具体文档类型"]
}}
"""

    @classmethod
    def _normalize_creation_skill_analysis(
        cls, value: dict, document_title: str, document_content: str, doc_type: str
    ) -> dict:
        if not isinstance(value, dict):
            raise ValueError("技能分析结果不是对象")
        fallback = cls._fallback_creation_skill_analysis(document_title, document_content, doc_type)

        def clean_text(key: str, maximum: int) -> str:
            text = str(value.get(key) or fallback[key]).strip()
            return cls._generalize_skill_text(
                text[:maximum],
                document_title,
                document_content,
                str(fallback[key]),
            )

        def clean_list(key: str, maximum_items: int, item_maximum: int) -> list[str]:
            raw = value.get(key)
            items = raw if isinstance(raw, list) else fallback[key]
            fallback_items = fallback[key]
            cleaned = []
            for index, item in enumerate(items):
                text = cls._coerce_skill_list_item(key, item)
                if not text:
                    continue
                fallback_item = str(
                    fallback_items[min(index, len(fallback_items) - 1)]
                )
                cleaned.append(
                    cls._generalize_skill_text(
                        text[:item_maximum],
                        document_title,
                        document_content,
                        fallback_item,
                    )
                )
            return cleaned[:maximum_items] or fallback[key]

        raw_examples = value.get("field_examples")
        examples = raw_examples if isinstance(raw_examples, dict) else fallback["field_examples"]

        def clean_examples(key: str) -> list[str]:
            raw = examples.get(key)
            items = raw if isinstance(raw, list) else fallback["field_examples"][key]
            if key == "common_titles":
                # 标题示例以从源标题结构确定性生成的完整仿写为准，模型只补充，
                # 避免机械脱敏产出“从某视角看，目标对象”一类残句。
                items = [*fallback["field_examples"][key], *items]
            cleaned = []
            for index, item in enumerate(items):
                text = cls._coerce_skill_list_item(key, item)
                if key == "common_titles":
                    text = cls._compact_skill_placeholders(text)
                    if not cls._is_complete_skill_heading_example(text):
                        continue
                if not text:
                    continue
                fallback_examples = fallback["field_examples"][key]
                fallback_example = fallback_examples[
                    min(index, len(fallback_examples) - 1)
                ]
                cleaned.append(
                    cls._generalize_skill_text(
                        text[:500],
                        document_title,
                        document_content,
                        fallback_example,
                        overlap_window=28 if key == "common_titles" else 14,
                    )
                )
            cleaned = cls._distinct_skill_items(cleaned, 6)
            minimum = 3 if key in ("common_titles", "writing_guidelines") else 1
            if len(cleaned) < minimum:
                cleaned = cls._distinct_skill_items(
                    [*cleaned, *fallback["field_examples"][key]], 6
                )
            return cleaned or fallback["field_examples"][key]

        title_design = cls._distinct_skill_items(
            [
                *clean_list("common_titles", 12, 80),
                *fallback["common_titles"],
            ],
            8,
        )
        if len(title_design) < 4:
            title_design = fallback["common_titles"]
        text_style = clean_text("text_style", 2000)
        if len(text_style) < 400:
            text_style = cls._merge_skill_prose(
                text_style,
                fallback["text_style"],
                2000,
            )
        diagram_style = clean_text("diagram_style", 1200)
        if len(diagram_style) < 400:
            diagram_style = cls._merge_skill_prose(
                diagram_style,
                fallback["diagram_style"],
                1200,
            )
        writing_guidelines = cls._distinct_skill_items(
            [
                *clean_list("writing_guidelines", 5, 240),
                *fallback["writing_guidelines"],
            ],
            8,
        )
        field_examples = {
            key: clean_examples(key)
            for key in (
                "common_titles",
                "text_style",
                "diagram_style",
                "writing_guidelines",
            )
        }
        # title_style 是旧版协议键。新版本不再生成独立内容，仅复制标题设计风格，
        # 让旧客户端仍能读取，同时避免在新界面和创作指令中出现重复字段。
        legacy_title_style = "；".join(title_design)[:1200]
        field_examples["title_style"] = list(field_examples["common_titles"])
        raw_example_document = str(
            value.get("example_document") or fallback["example_document"]
        ).strip()[:12000]
        # 阿拉伯数字编号本身不携带业务信息，先转成无序列表，避免一份合格长文
        # 仅因模型习惯写“1.”而整体退回通用示例。
        raw_example_document = re.sub(
            r"(?m)^(\s*)\d+(?:\.\d+)*[.、]\s+",
            r"\1- ",
            raw_example_document,
        )
        example_document = cls._generalize_skill_text(
            raw_example_document,
            document_title,
            document_content,
            fallback["example_document"],
            overlap_window=40,
        )
        if not cls._is_complete_skill_example_document(example_document):
            example_document = fallback["example_document"]
        distinctive_sections = cls._normalize_distinctive_sections(
            value.get("distinctive_sections"),
            fallback["distinctive_sections"],
            document_title,
            document_content,
        )
        skill_description = cls._normalize_skill_description(
            value.get("skill_description"),
            fallback["skill_description"],
            document_title,
            document_content,
        )
        execution_steps = cls._normalize_skill_execution_steps(
            value.get("execution_steps"),
            fallback["execution_steps"],
            document_title,
            document_content,
        )

        return {
            "title": cls._normalize_creation_skill_title(
                clean_text("title", 80), document_title, document_content, doc_type
            ),
            "summary": clean_text("summary", 400),
            "common_titles": title_design,
            "title_style": legacy_title_style,
            "text_style": text_style,
            "diagram_style": diagram_style,
            "writing_guidelines": writing_guidelines,
            "distinctive_sections": distinctive_sections,
            "section_headings": cls._default_skill_section_headings(),
            "field_examples": field_examples,
            "example_document": example_document,
            "skill_description": skill_description,
            "execution_steps": execution_steps,
            "suggested_category_keywords": clean_list("suggested_category_keywords", 8, 80),
        }

    @classmethod
    def _normalize_skill_description(
        cls,
        value: object,
        fallback: dict,
        document_title: str,
        document_content: str,
    ) -> dict:
        raw = value if isinstance(value, dict) else {}

        def clean_text(candidate: object, fallback_text: str, maximum: int) -> str:
            text = str(candidate or fallback_text).strip()[:maximum]
            return cls._generalize_skill_text(
                text,
                document_title,
                document_content,
                fallback_text,
            )

        def clean_items(key: str, maximum_items: int, item_maximum: int) -> list[str]:
            fallback_items = fallback.get(key) or []
            items = raw.get(key)
            source = items if isinstance(items, list) else fallback_items
            cleaned = [
                clean_text(
                    item,
                    str(fallback_items[min(index, len(fallback_items) - 1)])
                    if fallback_items
                    else "",
                    item_maximum,
                )
                for index, item in enumerate(source)
                if str(item or "").strip()
            ]
            return cls._distinct_skill_items(cleaned, maximum_items) or list(fallback_items)

        return {
            "purpose": clean_text(raw.get("purpose"), str(fallback["purpose"]), 1200),
            "document_types": clean_items("document_types", 12, 120),
            "problems": clean_items("problems", 12, 240),
            "domains": clean_items("domains", 12, 120),
            "deliverables": clean_items("deliverables", 12, 240),
        }

    @classmethod
    def _normalize_skill_execution_steps(
        cls,
        value: object,
        fallback: list[dict],
        document_title: str,
        document_content: str,
    ) -> list[dict]:
        allowed_agents = {
            "industry_research_agent",
            "data_analysis_agent",
            "solution_design_agent",
            "document_writer_agent",
            "quality_review_agent",
        }
        allowed_tools = {
            "memory_search",
            "internet_search",
            "data_search",
            "webpage_scrape",
            "github_search",
            "plantuml_diagram",
        }
        raw_steps = value if isinstance(value, list) else fallback
        normalized: list[dict] = []
        for index, item in enumerate(raw_steps[:12]):
            if not isinstance(item, dict):
                continue
            fallback_step = fallback[min(index, len(fallback) - 1)] if fallback else {}
            raw_id = str(item.get("id") or fallback_step.get("id") or f"step-{index + 1}")
            step_id = re.sub(r"[^a-z0-9_-]+", "-", raw_id.lower()).strip("-_")[:80]
            if not step_id:
                step_id = f"step-{index + 1}"

            def clean_step_text(key: str, maximum: int) -> str:
                fallback_text = str(fallback_step.get(key) or "")
                return cls._generalize_skill_text(
                    str(item.get(key) or fallback_text).strip()[:maximum],
                    document_title,
                    document_content,
                    fallback_text,
                )

            def resources(key: str, allowed: Optional[set[str]] = None) -> list[str]:
                raw = item.get(key)
                source = raw if isinstance(raw, list) else fallback_step.get(key, [])
                result = []
                for candidate in source:
                    resource_id = str(candidate or "").strip()[:80]
                    if not resource_id or (allowed is not None and resource_id not in allowed):
                        continue
                    if resource_id not in result:
                        result.append(resource_id)
                return result[:8]

            title = clean_step_text("title", 80)
            objective = clean_step_text("objective", 500)
            output = clean_step_text("output", 240)
            if not title or not objective or not output:
                continue
            tools = resources("tools", allowed_tools)
            agents = resources("agents", allowed_agents)
            tools = tools[:2]
            agents = agents[: max(0, 4 - len(tools))]
            normalized.append(
                {
                    "id": step_id,
                    "title": title,
                    "objective": objective,
                    "output": output,
                    "agents": agents,
                    "skills": resources("skills"),
                    "tools": tools,
                }
            )
        return normalized or fallback

    @staticmethod
    def _coerce_skill_list_item(key: str, item: object) -> str:
        """把模型误写的对象项转换为自然语言，永不暴露 Python dict repr。"""
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""

        def first(*names: str) -> str:
            for name in names:
                value = item.get(name)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()
            return ""

        if key in ("common_titles", "title_style"):
            level = first("level", "层级", "position", "位置")
            pattern = first("pattern", "骨架", "rule", "规则", "title", "标题")
            boundary = first("boundary", "usage", "适用位置", "说明")
            if pattern:
                prefix = f"{level}：" if level else ""
                suffix = f"；{boundary}" if boundary else ""
                return f"{prefix}采用“{pattern}”的标题骨架{suffix}"
        if key == "writing_guidelines":
            phrase = first("phrase", "term", "wording", "短语", "话术")
            usage = first("role", "usage", "effect", "作用", "说明")
            if phrase and usage:
                return f"习惯用“{phrase}”{usage}"
            return phrase or usage
        if key == "suggested_category_keywords":
            return first("path", "keyword", "name", "value", "类目")
        return first("example", "text", "content", "value", "示例")

    @classmethod
    def _normalize_distinctive_sections(
        cls,
        raw: object,
        fallback: list[dict],
        document_title: str,
        document_content: str,
    ) -> list[dict]:
        items = raw if isinstance(raw, list) else fallback
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            title = cls._first_skill_mapping_text(item, "title", "name", "heading")
            description = cls._first_skill_mapping_text(
                item, "description", "analysis", "characteristic", "why"
            )
            guidance = cls._first_skill_mapping_text(
                item, "guidance", "instruction", "how_to", "usage"
            )
            raw_examples = item.get("examples")
            if not isinstance(raw_examples, list):
                raw_examples = [item.get("example")] if item.get("example") else []

            title = cls._generalize_skill_text(
                title[:80], document_title, document_content, ""
            )
            description = cls._generalize_skill_text(
                description[:1200], document_title, document_content, ""
            )
            guidance = cls._generalize_skill_text(
                guidance[:1200], document_title, document_content, ""
            )
            examples = cls._distinct_skill_items(
                [
                    cls._generalize_skill_text(
                        cls._coerce_skill_list_item("examples", example)[:800],
                        document_title,
                        document_content,
                        "",
                        overlap_window=28,
                    )
                    for example in raw_examples
                    if cls._coerce_skill_list_item("examples", example)
                ],
                6,
            )
            fingerprint = re.sub(r"[\s\W_]+", "", title, flags=re.UNICODE)
            if (
                not title
                or not description
                or not guidance
                or not examples
                or fingerprint in seen
            ):
                continue
            seen.add(fingerprint)
            normalized.append(
                {
                    "title": title,
                    "description": description,
                    "guidance": guidance,
                    "examples": examples,
                }
            )
            if len(normalized) >= 6:
                break
        return normalized or fallback

    @staticmethod
    def _first_skill_mapping_text(value: dict, *keys: str) -> str:
        for key in keys:
            item = value.get(key)
            if isinstance(item, (str, int, float)) and str(item).strip():
                return str(item).strip()
        return ""

    @staticmethod
    def _distinct_skill_items(items: list[str], maximum: int) -> list[str]:
        result: list[str] = []
        fingerprints: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            fingerprint = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
            if not fingerprint or fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            result.append(text)
            if len(result) >= maximum:
                break
        return result

    @staticmethod
    def _merge_skill_prose(primary: str, fallback: str, maximum: int) -> str:
        first = str(primary or "").strip()
        second = str(fallback or "").strip()
        if not first:
            return second[:maximum]
        if not second or second in first:
            return first[:maximum]
        return f"{first}\n\n执行配方：{second}"[:maximum]

    @staticmethod
    def _compact_skill_placeholders(value: str) -> str:
        """压缩脱敏产生的连续占位词，并拒绝只剩占位词的伪标题。"""
        text = str(value or "").strip()
        text = re.sub(
            r"(?:目标对象[\s·—_:：/\\-]*){2,}",
            "目标对象",
            text,
        )
        text = re.sub(
            r"(?:相关角色[\s·—_:：/\\-]*){2,}",
            "相关角色",
            text,
        )
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        return re.sub(r"\s{2,}", " ", text).strip(" ：:·—_-/\\")

    @staticmethod
    def _is_complete_skill_heading_example(value: str) -> bool:
        text = str(value or "").strip()
        if len(re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)) < 4:
            return False
        if re.search(r"(?:目标对象|相关角色|相关团队)[的之]?$", text):
            return False
        if re.search(r"(?:看|关于|针对|面向)[，,:：]?$", text):
            return False
        return not bool(re.fullmatch(r"(?:目标对象|相关角色|相关团队)+", text))

    @staticmethod
    def _is_complete_skill_example_document(value: str) -> bool:
        text = str(value or "").strip()
        if len(text) < 1000:
            return False
        if len(re.findall(r"(?m)^#\s+\S", text)) != 1:
            return False
        if len(re.findall(r"(?m)^##\s+\S", text)) < 6:
            return False
        body_blocks = [
            block.strip()
            for block in re.split(r"\n\s*\n", text)
            if block.strip() and not block.lstrip().startswith("#")
        ]
        return len([block for block in body_blocks if len(block) >= 40]) >= 8

    @staticmethod
    def _generalize_skill_text(
        candidate: str,
        document_title: str,
        document_content: str,
        fallback: str,
        overlap_window: int = 14,
    ) -> str:
        """拒绝明显的组织线索和大段原文重合，避免提炼结果反向披露来源。"""
        text = str(candidate or "").strip()
        if not text:
            return fallback
        if re.search(r"\d", text):
            return fallback
        if CreationService._contains_named_private_marker(text):
            return fallback

        compact_candidate = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
        compact_source = re.sub(
            r"[\s\W_]+",
            "",
            f"{document_title}\n{document_content}",
            flags=re.UNICODE,
        )
        if len(compact_candidate) >= overlap_window:
            window = overlap_window
            for index in range(0, len(compact_candidate) - window + 1):
                if compact_candidate[index:index + window] in compact_source:
                    return fallback
        return text

    @staticmethod
    def _contains_named_private_marker(value: str) -> bool:
        if "有限责任公司" in value or "股份有限公司" in value:
            return True
        generic_prefixes = (
            "跨",
            "多",
            "多个",
            "各",
            "相关",
            "某",
            "示例",
            "通用",
            "不同",
            "该",
            "由",
            "与",
            "和",
            "及",
            "为",
            "在",
            "向",
            "对",
            "于",
        )
        for marker in ("事业群", "事业部", "研发中心", "产品部", "项目组", "工作组"):
            start = 0
            while True:
                index = value.find(marker, start)
                if index < 0:
                    break
                prefix = value[:index].rstrip()
                if prefix and not prefix.endswith(generic_prefixes):
                    return True
                start = index + len(marker)
        return False

    @staticmethod
    def _normalize_creation_skill_title(
        candidate: str, document_title: str, document_content: str, doc_type: str
    ) -> str:
        """把模型命名收敛为不含具体组织的可复用文档用途。"""
        # 文档正文可能包含案例、引用或多轮 Bake 追加内容，不能仅因正文偶然
        # 出现“复盘”“会议”等词就覆盖整枚技能的用途。用途特判只看标题和
        # 已确认的文档类型，正文仍用于模型提炼具体写法。
        source_text = f"{document_title}\n{doc_type}"
        if (
            re.search(r"跨部门|跨团队|多团队", source_text)
            and re.search(r"技术|架构|研发|系统", source_text)
            and re.search(r"会议|沟通|评审|纪要", source_text)
        ):
            return "跨部门技术沟通会文档"
        if re.search(r"跨部门|跨团队|多团队", source_text) and re.search(
            r"会议|沟通|协作|纪要", source_text
        ):
            return "跨部门协作会议文档"
        if re.search(r"架构评审|技术评审|方案评审", source_text):
            return "技术方案评审文档"
        if re.search(r"复盘|总结会", source_text):
            return "项目复盘总结文档"
        if re.search(r"客户|交付|实施", source_text) and re.search(
            r"沟通|汇报|会议", source_text
        ):
            return "客户交付沟通文档"
        if re.search(r"整体技术方案", source_text):
            return "运行平台整体技术方案"
        if re.search(r"技术架构|架构设计", source_text):
            return "技术架构设计文档"
        if re.search(r"技术方案", source_text):
            return "技术方案文档"
        if re.search(r"评测接入", source_text):
            return "评测接入文档"

        normalized = str(candidate or "").strip()
        organization_pattern = re.compile(
            r"[\w·-]{1,16}?(?:事业群|事业部|委员会|项目组|工作组|部门|团队|小组|中心|部)"
        )
        normalized = organization_pattern.sub("", normalized)
        normalized = re.sub(r"(?:创作|写作)\s*Skill$", "", normalized, flags=re.I)
        normalized = re.sub(r"Skill$", "", normalized, flags=re.I)
        normalized = re.sub(r"沟通会(?:会议)?纪要(?:撰写)?指南$", "沟通会文档", normalized)
        normalized = re.sub(r"会议纪要(?:撰写)?指南$", "会议文档", normalized)
        normalized = normalized.strip(" \t\r\n·—_:：-")
        if re.search(r"复盘|总结", normalized) and not re.search(
            r"复盘|总结", source_text
        ):
            normalized = ""
        if len(normalized) >= 4 and not organization_pattern.search(normalized):
            return normalized[:80]

        base_type = (doc_type or "专业文档").strip()
        if re.search(r"文档|方案|报告|规范|计划|SOP$", base_type, flags=re.I):
            return base_type[:80]
        return f"{base_type}文档"[:80]

    @classmethod
    def _fallback_creation_skill_analysis(cls, title: str, content: str, doc_type: str) -> dict:
        source_headings = cls._extract_skill_source_headings(content, title)
        structure = []
        for heading in source_headings:
            cleaned = cls._canonical_skill_heading(heading)
            if cleaned and cleaned not in structure:
                structure.append(cleaned[:160])
            if len(structure) >= 10:
                break
        if not structure:
            structure = ["背景与目标", "核心分析", "方案设计", "实施与风险", "结论与后续"]

        base_type = (doc_type or "专业文档").strip()
        title_design = cls._describe_heading_design_style(source_headings, content)
        heading_examples = cls._heading_style_examples(source_headings, structure, title)
        text_style = cls._describe_writing_flow(structure, content)
        voice_style, voice_examples = cls._extract_voice_style(content)
        diagram_style, diagram_examples = cls._diagram_generation_style(content)
        distinctive_sections = cls._fallback_distinctive_sections(content)
        abstract_title = cls._normalize_creation_skill_title(base_type, title, content, doc_type)
        field_examples = cls._default_skill_field_examples()
        field_examples.update(
            {
                "common_titles": heading_examples,
                "title_style": list(heading_examples),
                "text_style": [cls._fallback_flow_example(structure, content)],
                "diagram_style": diagram_examples,
                "writing_guidelines": voice_examples,
            }
        )
        skill_description = cls._fallback_skill_description(
            abstract_title,
            base_type,
            title,
            content,
        )
        execution_steps = cls._fallback_skill_execution_steps(
            abstract_title,
            base_type,
            title,
            content,
        )
        return {
            "title": abstract_title,
            "summary": f"适合需要创作{abstract_title}的专业人员，用于直接复刻源文档的子标题句式、章节推进、惯用话术和代码生图方式。"[:400],
            "common_titles": title_design,
            "title_style": "；".join(title_design)[:1200],
            "text_style": text_style,
            "diagram_style": diagram_style,
            "writing_guidelines": voice_style,
            "distinctive_sections": distinctive_sections,
            "section_headings": cls._default_skill_section_headings(),
            "field_examples": field_examples,
            "example_document": cls._fallback_skill_example_document(
                base_type,
                source_headings,
                content,
            ),
            "skill_description": skill_description,
            "execution_steps": execution_steps,
            "suggested_category_keywords": [base_type],
        }

    @staticmethod
    def _fallback_skill_description(
        abstract_title: str,
        doc_type: str,
        document_title: str,
        content: str,
    ) -> dict:
        evidence = f"{document_title}\n{doc_type}\n{content[:6000]}"
        domains = []
        domain_rules = (
            (r"金融|银行|支付|信贷|保险|证券|基金", "金融"),
            (r"制造|工艺|产线|工业|质量", "制造"),
            (r"电商|零售|商品|订单|履约", "电商零售"),
            (r"产品|需求|用户体验|运营", "产品与运营"),
            (r"技术|架构|系统|接口|软件|数据平台", "软件与技术"),
            (r"咨询|行业研究|市场研究", "咨询与研究"),
            (r"医疗|临床|药品|康养", "医疗健康"),
            (r"教育|课程|教学|培训", "教育培训"),
        )
        for pattern, domain in domain_rules:
            if re.search(pattern, evidence, re.I) and domain not in domains:
                domains.append(domain)
        if not domains:
            domains.append("专业办公")

        if re.search(r"研究|调研|分析|报告", evidence, re.I):
            problem = "把分散资料和证据转化为有依据、可比较、可形成结论的分析"
        elif re.search(r"方案|架构|设计|规划|建设", evidence, re.I):
            problem = "把目标、约束和关键取舍转化为可评审、可执行、可验证的方案"
        elif re.search(r"复盘|总结|纪要", evidence, re.I):
            problem = "从过程记录中提炼事实、判断、行动项和后续验证方式"
        else:
            problem = "把零散需求与事实组织成结构清晰、可直接使用的专业文档"

        return {
            "purpose": (
                f"用于在需要创作{abstract_title}时，复用源文档形成事实、分析、"
                "方案和交付结论的方法，同时保持其标题、结构与表达特征。"
            )[:1200],
            "document_types": [abstract_title[:120]],
            "problems": [problem[:240]],
            "domains": domains[:12],
            "deliverables": [
                f"一份结构完整、依据清楚并包含后续动作的{abstract_title}"[:240]
            ],
        }

    @staticmethod
    def _fallback_skill_execution_steps(
        abstract_title: str,
        doc_type: str,
        document_title: str,
        content: str,
    ) -> list[dict]:
        evidence = f"{document_title}\n{doc_type}\n{content[:6000]}"
        steps = [
            {
                "id": "collect-context",
                "title": "收集需求与事实",
                "objective": "明确创作目标、读者、范围、已有资料和不能推断的事实边界。",
                "output": "需求清单和有依据的事实材料",
                "agents": [],
                "skills": [],
                "tools": ["memory_search"],
            }
        ]
        if re.search(r"行业|市场|竞品|研究|调研|政策|趋势", evidence, re.I):
            steps.append(
                {
                    "id": "research-industry",
                    "title": "开展行业调研",
                    "objective": "补充外部环境、通行做法和来源可追溯的行业证据。",
                    "output": "带来源的行业事实、趋势与可比较案例",
                    "agents": ["industry_research_agent"],
                    "skills": [],
                    "tools": ["internet_search"],
                }
            )
        if re.search(r"数据|指标|统计|趋势|成本|收益|测算|分析", evidence, re.I):
            steps.append(
                {
                    "id": "analyze-data",
                    "title": "分析数据与证据",
                    "objective": "核对数据口径，识别关键关系、差异和支撑结论的证据。",
                    "output": "有依据的数据判断和口径说明",
                    "agents": ["data_analysis_agent"],
                    "skills": [],
                    "tools": ["data_search", "webpage_scrape"],
                }
            )
        if re.search(r"方案|架构|设计|规划|建设|实施", evidence, re.I):
            steps.append(
                {
                    "id": "design-solution",
                    "title": "设计方案",
                    "objective": "把目标、约束和证据转化为有边界、有取舍、有验证方式的方案。",
                    "output": "方案结构、关键设计、实施路径和风险控制",
                    "agents": ["solution_design_agent"],
                    "skills": [],
                    "tools": ["plantuml_diagram"]
                    if re.search(r"架构|流程|链路|交互|模块", evidence, re.I)
                    else [],
                }
            )
        steps.extend(
            [
                {
                    "id": "draft-document",
                    "title": "撰写完整文档",
                    "objective": f"依据前序产出和 Skill 的风格指纹完成{abstract_title}，不补造事实。",
                    "output": "可继续编辑的完整 Markdown 文档",
                    "agents": ["document_writer_agent"],
                    "skills": [],
                    "tools": [],
                },
                {
                    "id": "review-delivery",
                    "title": "审校并交付",
                    "objective": "检查目标回应、事实依据、结构完整、术语一致和行动可执行性。",
                    "output": "通过质量检查的最终文档",
                    "agents": ["quality_review_agent"],
                    "skills": [],
                    "tools": [],
                },
            ]
        )
        return steps[:8]

    @staticmethod
    def _extract_skill_source_headings(content: str, document_title: str) -> list[str]:
        markdown_matches = re.findall(
            r"^\s{0,3}(#{1,6})\s+(.+?)\s*$",
            content,
            flags=re.MULTILINE,
        )
        candidates: list[tuple[int, str]] = [
            (
                len(markers),
                re.sub(r"[*_`#]", "", heading).strip().rstrip("：:"),
            )
            for markers, heading in markdown_matches
        ]
        if not candidates:
            numbered = re.findall(
                r"^\s*(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*[.、]\s*)(.{2,80})$",
                content,
                flags=re.MULTILINE,
            )
            candidates = [(2, heading.strip().rstrip("：:")) for heading in numbered]

        normalized_title = re.sub(r"[\s\W_]+", "", document_title, flags=re.UNICODE)
        result: list[str] = []
        fingerprints: set[str] = set()
        for _, heading in candidates:
            if not heading:
                continue
            fingerprint = re.sub(r"[\s\W_]+", "", heading, flags=re.UNICODE)
            if not fingerprint or fingerprint == normalized_title or fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            result.append(heading[:120])
            if len(result) >= 24:
                break
        return result

    @staticmethod
    def _describe_heading_design_style(headings: list[str], content: str) -> list[str]:
        """从实际子标题归纳可复刻的命名指纹，不虚构常见标题。"""
        if not headings:
            return [
                "层级边界：源文档没有可识别的独立子标题；仿写时只在话题明确切换处增加标题",
                "句式骨架：新增标题用“内容对象＋章节动作”的短名词结构，不写完整结论句",
                "使用边界：连续论述优先靠段落承接，不为了显得完整而强行拆成多层目录",
                "措辞选择：标题直接概括下一段承担的职责，不使用宣传口号或空泛形容词",
            ]

        lengths = [len(re.sub(r"\s+", "", item)) for item in headings]
        average = sum(lengths) / len(lengths)
        length_style = (
            "长度节奏：子标题以四到八字的短名词结构为主；同层标题保持相近长度，便于扫读"
            if average <= 8
            else "长度节奏：子标题多为带限定语的中等长度短句；先限定对象或范围，再落到章节动作"
        )
        observations = [length_style]
        joined = "\n".join(headings)
        if re.search(r"[与及和]", joined):
            observations.append("并列骨架：使用“名词或动作＋与/及＋名词或结果”；只并列同一章节内同层级的两个重点")
        if re.search(r"[：:]", joined):
            observations.append("冒号骨架：使用“主题＋冒号＋具体判断或动作”；冒号前定位话题，冒号后给阅读重点")
        if re.search(r"[？?]|为何|为什么|如何|怎么", joined):
            observations.append("问句骨架：把待回答的问题直接写入标题；正文首段必须紧接着给出判断或方案")
        if re.search(r"从.{1,12}(?:视角|角度|层面).{0,4}看", joined):
            observations.append("视角骨架：使用“从某一视角看，目标对象”；只在切换分析立场时使用，不当通用前缀")
        if re.search(r"建设|设计|实现|落地|优化|验证|复盘|说明|分析", joined):
            observations.append("动作标记：保留“设计、实现、验证、复盘”等任务词；用动作说明章节职责，不用抽象形容词")
        if re.search(r"背景|目标|现状|方案|风险|验证|结论|后续", joined):
            observations.append("路线标题：直接使用背景、目标、方案、风险、验证、后续等内容角色词，让目录呈现推进顺序")
        if re.search(r"[A-Za-z]{2,}", joined):
            observations.append("术语嵌入：英文技术词作为精确对象嵌入中文标题；保留必要术语，不把整句改成英文口号")
        if len(observations) < 4:
            observations.append("层级一致：同层标题保持相同语法结构，不在名词短语、问句和完整结论句之间随意切换")
        return list(dict.fromkeys(observations))[:8]

    @classmethod
    def _heading_style_examples(
        cls, headings: list[str], structure: list[str], document_title: str
    ) -> list[str]:
        """最小替换标题中的敏感对象，保留源标题的词序、动词和标点。"""
        examples: list[str] = []
        generic_prefixes = ("总体", "核心", "背景", "现状", "目标", "范围", "风险", "结论", "后续")
        sensitive_tail = re.compile(
            r"^(.{2,16}?)(迁移方案|优化方案|实施方案|设计方案|架构设计|流程设计|复盘报告|分析报告)$"
        )
        preserved_terms = {
            "api", "sdk", "os", "runtime", "agent", "ai", "ui", "ux", "http", "https"
        }
        title_fragments = [
            fragment
            for fragment in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", document_title)
            if fragment.lower() not in preserved_terms
        ]
        chinese_core = re.sub(
            r"(?:整体)?(?:技术)?(?:方案|文档|报告|设计|规划|说明|手册|指南)$",
            "",
            re.sub(r"[A-Za-z0-9_\-\s]+", "", document_title),
        ).strip()
        if len(chinese_core) >= 2:
            title_fragments.append(chinese_core)

        source_examples = [document_title, *headings]
        for heading in source_examples:
            candidate = re.sub(r"`[^`]+`|“[^”]+”|「[^」]+」", "目标对象", heading)
            candidate = re.sub(r"\d+(?:\.\d+)*", "阶段", candidate)
            candidate = re.sub(
                r"[\w·-]{1,16}?(?:事业群|事业部|研发中心|产品部|项目组|工作组)",
                "相关团队",
                candidate,
            )
            for fragment in sorted(title_fragments, key=len, reverse=True):
                candidate = re.sub(
                    re.escape(fragment),
                    "协作工作台",
                    candidate,
                    flags=re.IGNORECASE,
                )
            candidate = re.sub(
                r"\b[A-Z][A-Za-z0-9_-]{2,}\b",
                lambda match: (
                    match.group(0)
                    if match.group(0).lower() in preserved_terms
                    else "协作工作台"
                ),
                candidate,
            )
            candidate = cls._compact_skill_placeholders(candidate)
            matched = sensitive_tail.match(candidate)
            if matched and not matched.group(1).startswith(generic_prefixes):
                candidate = f"协作工作台{matched.group(2)}"
            if re.fullmatch(
                r"从.{1,12}(?:视角|角度|层面)看[，,:：]?(?:协作工作台|目标对象)",
                candidate,
            ):
                candidate = f"{candidate}的角色与边界"
            if re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]*\s*[：:]\s*(?:协作工作台|目标对象)",
                candidate,
            ):
                candidate = f"{candidate}的调度边界"
            if cls._contains_named_private_marker(candidate):
                candidate = cls._canonical_skill_heading(candidate)
            candidate = cls._compact_skill_placeholders(candidate)
            semantic_remainder = re.sub(
                r"目标对象|相关角色|相关团队|阶段|[\s\W_]+",
                "",
                candidate,
                flags=re.UNICODE,
            )
            if (
                len(semantic_remainder) >= 2
                and cls._is_complete_skill_heading_example(candidate)
                and candidate not in examples
            ):
                examples.append(candidate[:80])
            if len(examples) >= 6:
                break
        if not examples:
            examples = structure[:3]
        return examples

    @staticmethod
    def _fallback_distinctive_sections(content: str) -> list[dict]:
        """从可观察证据生成固定字段之外的特色亮点，最多保留四项。"""
        sections: list[dict] = []

        def add(title: str, description: str, guidance: str, examples: list[str]) -> None:
            if len(sections) < 4:
                sections.append(
                    {
                        "title": title,
                        "description": description,
                        "guidance": guidance,
                        "examples": examples,
                    }
                )

        if re.search(r"定义|可以理解为|核心目标|换言之", content[:4000]):
            add(
                "定义先行的概念建立",
                "源文档在展开方案前先解释核心对象是什么、解决什么问题，再用核心目标限定后续讨论，定义本身承担阅读入口。",
                "核心对象首次出现时，先用一句通俗类比降低理解门槛，再补一句职责边界；随后列出目标或非目标。仅在术语可能被不同角色误解时使用。",
                [
                    "协作工作台可以理解为任务流转的统一入口：它连接请求、处理角色与结果证据，但不替代各环节的专业判断。",
                    "核心目标是让接手者在不额外询问的情况下，判断当前状态、下一步动作与完成依据。",
                ],
            )
        label_count = len(re.findall(r"(?:\*\*)?[^。\n：:]{2,18}(?:\*\*)?[：:]", content))
        if label_count >= 3:
            add(
                "短标签驱动的信息展开",
                "源文档反复用短标签加冒号定位信息角色，再在同一行或后续短段中补充解释，使高密度内容仍能快速扫描。",
                "标签控制在一个概念或动作内，并让同组标签保持同一语法类型；冒号后先给结论，再补条件。连续论证不要强行拆成标签。",
                [
                    "职责边界：维护角色只确认自己能够验证的资源状态，不代替申请角色补写用途。",
                    "完成证据：释放动作必须留下可观察结果，无法确认时回到复核状态。",
                ],
            )
        if re.search(r"```(?:plantuml|mermaid)", content, re.I):
            add(
                "代码图示与正文同词复现",
                "源文档把可执行图示代码放在解释之后，并让节点、分组和连线继续使用正文已经建立的术语，图不是独立装饰。",
                "先用正文说明阅读顺序和关键关系，再给 PlantUML 或 Mermaid 代码；图中只保留正文已有对象，连线使用动作词，图后补充异常或边界。",
                [
                    "正文先说明申请、确认与释放的主链路，再用 PlantUML 活动图纵向排列动作，并把跨角色步骤放入对应泳道。"
                ],
            )
        if len(re.findall(r"(?m)^\s*---+\s*$", content)) >= 2:
            add(
                "分隔线控制议题切换",
                "源文档用独立分隔线标记较大的议题或文档入口切换，让读者在长内容中明确感知上下文已经重置。",
                "只在讨论对象或交付目标发生明显变化时使用分隔线；分隔线后重新给出标题或一句入口判断，不把它当作普通段落装饰。",
                [
                    "完成总体方案说明后使用分隔线，下一部分以“评测接入：先明确入口与返回结果”重新建立阅读上下文。"
                ],
            )
        return sections

    @staticmethod
    def _describe_writing_flow(structure: list[str], content: str) -> str:
        route = " → ".join(structure)
        list_count = len(re.findall(r"^\s*(?:[-*+]|\d+[.、])\s+", content, re.MULTILINE))
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
        avg_paragraph = sum(map(len, paragraphs)) / max(1, len(paragraphs))
        paragraph_style = (
            "段内承载较多解释，通常先给判断，再连续补充原因、边界和落法"
            if avg_paragraph > 120
            else "用短段落推进，一个段落只承担一个判断、动作或补充说明"
        )
        list_style = (
            "遇到并列动作或条件时切成列表，列表项保持同一语法起点"
            if list_count
            else "主要依靠连续段落而非清单推进，段与段之间用因果或递进关系承接"
        )
        opener = (
            "开篇先给定义或核心判断，再补适用范围，读者无需读完背景才知道文档要解决什么"
            if re.search(r"定义|可以理解为|核心目标|总体来看", content[:1200])
            else "开篇先交代问题角色、适用范围和目标，再进入分析，不用口号或宽泛行业背景铺垫"
        )
        label_style = (
            "信息展开时大量使用“短标签＋冒号＋解释”，标签负责定位信息类型，冒号后补依据或动作"
            if len(re.findall(r"\*\*[^*\n]{2,20}\*\*[：:]", content)) >= 2
            else "信息展开以完整判断句为主，只在同层信息需要快速扫描时切换为标签或列表"
        )
        transition_phrases = [
            phrase
            for phrase in ("需要说明的是", "具体而言", "基于此", "同时", "此外", "因此", "最后")
            if phrase in content
        ]
        transition_style = (
            f"章节与段落之间沿用“{'、'.join(transition_phrases[:4])}”等连接词，分别承担补充、递进或收束"
            if transition_phrases
            else "章节之间靠标题角色和前后因果自然承接，不额外堆叠“首先、其次、最后”等模板连接词"
        )
        return (
            f"章节路线：全文沿“{route}”推进，标题本身承担阅读导航；中段展开分析、方案或取舍，"
            f"末段必须落到验证和后续动作。开篇配方：{opener}。段内配方：{paragraph_style}，"
            f"通常先写本段判断，再补原因、边界和落法。列表条件：{list_style}，不要把相互依赖的论证"
            f"拆成彼此孤立的要点。信息密度：{label_style}。衔接方式：{transition_style}。"
            "段落节奏：定义、判断、依据和动作分别承担清晰职责；同一段出现多个转折时应拆段，但不要把一句完整论证切成口号。"
            "收束要求：结尾回看目标、给出可验证结果和下一步，不重复摘要，也不新增未经前文论证的判断。"
            "不可迁移项：只复刻标题句法、信息顺序和语气，不复制源文档的专名、事实、结论、日期或指标；"
            "源文档缺少证据的图示、列表和结论句也不能为了形式完整而补造。"
            "交付前自检：逐节确认标题是否预告正文职责、首段是否立即回应标题、并列项是否同构、结尾是否留下可执行动作与验证依据。"
        )

    @staticmethod
    def _extract_voice_style(content: str) -> tuple[list[str], list[str]]:
        phrase_roles = (
            ("需要说明的是", "引出边界、例外或容易误解的前提"),
            ("值得注意的是", "提示风险或需要读者停顿关注的信息"),
            ("具体而言", "把上一层判断拆成可执行细节"),
            ("基于此", "承接前文依据并转入结论或方案"),
            ("换言之", "用更直接的说法重述复杂判断"),
            ("总体来看", "在段落或章节末收束判断"),
            ("首先", "开启有顺序的论述或动作清单"),
            ("其次", "延续同层级的第二个论点"),
            ("最后", "收束一组论点并转入结论"),
            ("同时", "补充并行条件或同步动作"),
            ("此外", "增加独立但相关的补充信息"),
            ("因此", "从原因过渡到判断、动作或结果"),
            ("建议", "用克制语气提出行动"),
            ("需要", "直接声明必要动作或约束"),
            ("应当", "以规范性语气提出要求"),
            ("必须", "标记不可让步的硬约束"),
            ("优先", "表达取舍顺序而不使用夸张措辞"),
            ("避免", "用负向动作明确禁止项"),
            ("确保", "把动作落到预期结果"),
            ("明确", "要求把模糊对象、边界或责任说具体"),
        )
        matched_phrases = [
            (phrase, role)
            for phrase, role in phrase_roles
            if phrase in content
        ]
        styles = [
            (
                f"证据话术“{phrase}”：源文档用它{role}；复刻时把它放在承担同类职责的句首，"
                "后面紧接完整判断、条件或动作，不让短语单独成句；同一段只使用一次，"
                "没有对应逻辑关系时不要把它当作装饰性连接词。"
            )
            for phrase, role in matched_phrases[:5]
        ]

        if "：" in content:
            styles.append(
                "标点句式“短标签＋冒号＋解释”：源文档用冒号把信息角色和具体内容分开；"
                "复刻时标签保持短而同构，冒号后先写核心判断再补依据，适合定义、约束和并列说明；"
                "连续论证或因果链不要硬拆成标签。"
            )
        if "；" in content:
            styles.append(
                "长句节奏“分号切分同层判断”：源文档用分号承载彼此并列且各自完整的信息；"
                "复刻时让分号两侧保持相同语法起点和相近粒度，读完仍是一组判断；"
                "存在先后、因果或转折时应拆句，不用分号掩盖关系。"
            )
        if re.search(r"^\s*[-*+]\s+", content, re.MULTILINE):
            styles.append(
                "列表话术“动作词先行”：源文档把可并列扫描的动作、条件或结果写成列表；"
                "复刻时每项先用同类动词或名词短语点明职责，再补对象与边界，语气直接克制；"
                "相互依赖的论证仍保留连续段落。"
            )

        modal_words = [
            word for word in ("建议", "需要", "应当", "必须", "优先", "避免", "确保", "明确")
            if word in content
        ]
        if modal_words:
            styles.append(
                f"动作语气“{'、'.join(modal_words[:4])}”：源文档用这些词区分建议、必要动作、"
                "优先级与禁止项；复刻时把动作主体、作用对象和预期结果写全，强弱程度沿用原文证据；"
                "没有硬约束依据时不得把“建议”擅自升级为“必须”。"
            )

        if not matched_phrases:
            styles.insert(
                0,
                "话术证据边界：源文档没有识别出稳定反复出现的标志性短语，复刻时以原有句法、"
                "标点和动作词为准，不额外植入“首先、其次、综上”等模板过渡语；"
                "需要承接时直接写清前后判断的因果、递进或范围变化。"
            )

        styles.extend(
            [
                (
                    "句式节奏复刻：源文档以能够独立成立的陈述句承载判断，复刻时先写清谁对什么采取何种动作，"
                    "再用后句补原因、条件或结果；一个句子只保留一条主逻辑，出现多次转折时拆句，"
                    "但不把完整论证切成缺少主谓的口号。"
                ),
                (
                    "术语与指代控制：从源文档提取可公开复用的专业称呼后，为同一概念固定一种叫法，"
                    "后文只在指代对象明确时使用“该对象”“这一过程”等代词；"
                    "不得为了显得专业堆叠近义词，也不得把来源专名带入新的虚构主题。"
                ),
                (
                    "段落语气迁移：延续源文档先判断、再补依据与适用边界的完整陈述，"
                    "主语和动作保持明确，专业词首次出现时给出足够上下文；"
                    "只迁移表达顺序与语气，不复制来源中的专名、事实、指标和业务结论。"
                ),
                (
                    "话术交付自检：逐段检查连接词是否真的对应补充、递进、因果或收束，"
                    "动作词是否带清楚的执行对象与结果，列表项是否同构，强制语气是否有依据；"
                    "删去不承担信息作用的套话，并统一同一概念的称呼。"
                ),
            ]
        )
        styles = list(dict.fromkeys(styles))
        # 优先保留源文档中的短语证据，同时保证配方至少包含迁移边界和交付检查。
        if len(styles) > 8:
            styles = [*styles[:6], *styles[-2:]]
        while sum(len(item) for item in styles) > 700 and len(styles) > 5:
            styles.pop(-3)
        phrases = [
            phrase for phrase, _ in phrase_roles if phrase in content
        ][:3]
        examples = (
            [
                f"{phrase}，相关角色先确认适用边界，再推进后续动作。"
                for phrase in phrases
            ]
            if phrases
            else ["源文档没有稳定的惯用短语，仿写时不额外植入模板化套话。"]
        )
        return styles[:8], examples

    @staticmethod
    def _diagram_generation_style(content: str) -> tuple[str, list[str]]:
        lower = content.lower()
        if "```plantuml" in lower or "@startuml" in lower:
            choice = (
                "源文档存在 PlantUML 代码图示，继续使用 PlantUML，并根据正文实际关系选择组件图、"
                "时序图或活动图；默认保留源图从左到右的主阅读方向，用 package 或 rectangle 表达边界。"
            )
            example = "PlantUML：按正文层级用 package 分组，核心对象放在主轴上，关系箭头使用正文中的动作词标注。"
        elif "```mermaid" in lower:
            choice = (
                "源文档存在 Mermaid 代码图示，继续使用 Mermaid，并沿用 flowchart 或 "
                "sequenceDiagram 的表达方式；节点使用短名词，连线使用动作词，分组边界与正文层级对应。"
            )
            example = "Mermaid flowchart：主流程沿同一方向排列，分支只表达正文已经解释的判断条件。"
        elif re.search(r"时序图|调用链|交互顺序", content, re.I):
            choice = (
                "源文档以时间顺序解释交互，推荐 PlantUML sequence diagram；参与者按正文首次出现"
                "顺序排列，消息箭头使用动作词，异常或条件链路放入 alt 分组。"
            )
            example = "PlantUML 时序图：参与者按出现顺序排列，主链路使用实线箭头，条件分支放入 alt 区块。"
        elif re.search(r"架构图|组件图|分层|模块关系", content, re.I):
            choice = (
                "源文档存在分层、模块或依赖关系，推荐 PlantUML component diagram；"
                "同层对象横向对齐，使用 package 分组边界，只保留正文重点讨论的关键依赖。"
            )
            example = "PlantUML 组件图：用 package 表示层级，用 component 表示模块，依赖箭头标注正文中的关系动词。"
        elif re.search(r"流程图|步骤|流转|审批", content, re.I):
            choice = (
                "源文档存在步骤、流转或审批关系，推荐 PlantUML activity diagram；"
                "主流程从上到下排列，判断节点写成问题，角色发生切换时使用泳道。"
            )
            example = "PlantUML 活动图：主流程纵向排列，判断使用条件分支，跨角色动作放入对应泳道。"
        else:
            choice = (
                "源文档未识别到图示代码或图片说明，默认不生成图片；只有当对象关系、时间交互或"
                "条件流程仅靠连续文字难以准确理解时，才使用 PlantUML 补充组件图、时序图或活动图。"
            )
            example = "默认不生成图片；确需补图时使用 PlantUML，并只画正文已经说明的对象、边界和关系。"

        recipe = (
            f"证据与启用条件：{choice}"
            "选型判断：稳定依赖或分层关系用组件图，跨角色的先后消息用时序图，带判断与回退的动作链用活动图；"
            "同一张图只回答一个核心问题，无法明确图要解释什么时继续使用文字。"
            "信息筛选：先从正文提取已经定义的对象、边界、动作、条件和结果，再删去背景铺垫、评价性形容词、"
            "未被正文解释的内部细节与敏感事实；图中不得新增来源没有支持的节点、关系或结论。"
            "布局与阅读路径：主链路保持单一方向，核心对象放在视觉主轴，同层元素对齐，跨层关系通过分组边界表达；"
            "分支从触发点就近展开，避免箭头交叉和读者来回跳读。"
            "元素与标注：节点名称沿用正文中的短名词，箭头使用可执行的关系动词，条件写在分支或消息上，"
            "边界用 package、rectangle、subgraph 或泳道表示；同类元素必须采用同一种形状和命名粒度。"
            "视觉规则：使用暖灰、深棕与低饱和强调色区分层级，颜色只承担分组、状态或重点提示，不用渐变、阴影、"
            "装饰图标和无意义图例；正文术语、图中术语与标题保持完全一致。"
            "图文衔接：在图前先用一段话说明阅读方向、图要回答的问题和暂不覆盖的边界，图后只解释关键关系、"
            "异常分支及其对方案的影响，不逐节点复述图面。"
            "禁用边界与自检：不把大段正文塞进节点，不用一张图同时承载架构、时序和流程，不用图替代必要的决策依据；"
            "交付前检查代码能否渲染、方向是否唯一、连线是否有语义、术语是否一致、每个元素是否都能回指正文。"
        )
        return recipe, [example]

    @staticmethod
    def _fallback_flow_example(structure: list[str], content: str) -> str:
        opener = "首先，" if "首先" in content else ""
        connector = "基于此，" if "基于此" in content else "随后，"
        closer = "因此，" if "因此" in content else "最后，"
        return (
            f"{opener}先界定示例事项的目标与适用范围，并直接说明本次不处理的边界，"
            "让读者在进入方案前先形成同一问题定义。\n\n"
            f"{connector}按“{' → '.join(structure[:4])}”推进：每一节先给判断，"
            "再补充形成判断的依据、影响范围和具体落法；只有并列条件需要快速比较时才改用列表。\n\n"
            f"{closer}回到开篇目标，用可观察的验证结果收束判断，并明确后续动作、责任边界和复核方式。"
        )

    @staticmethod
    def _canonical_skill_heading(heading: str) -> str:
        """把源章节归并为通用章节角色，不保留项目、产品或组织名称。"""
        mappings = (
            (r"背景|现状|概述", "背景与目标"),
            (r"为什么|为何|原因|必要性|问题", "问题与原因"),
            (r"目标|范围", "目标与范围"),
            (r"约束|原则", "约束与设计原则"),
            (r"架构|总体设计", "总体方案"),
            (r"方案|策略|路径|落地", "方案设计"),
            (r"流程|步骤", "核心流程"),
            (r"功能|模块", "核心设计"),
            (r"接口|数据", "接口与数据"),
            (r"实施|计划|里程碑", "实施计划"),
            (r"风险|保障", "风险与保障"),
            (r"验证|验收|指标", "验证与验收"),
            (r"结论|总结|后续", "结论与后续"),
        )
        for pattern, canonical in mappings:
            if re.search(pattern, heading, re.I):
                return canonical
        return ""

    @staticmethod
    def _generic_common_titles(doc_type: str) -> list[str]:
        base = re.sub(r"(?:文档|报告)$", "", doc_type or "专业")
        return [
            f"{base}方案"[:80],
            f"{base}设计与实施说明"[:80],
            f"{base}复盘与后续行动"[:80],
        ]

    @staticmethod
    def _default_skill_section_headings() -> dict[str, str]:
        return {
            "common_titles": "标题设计风格",
            "title_style": "标题设计风格",
            "text_style": "行文设计思路",
            "diagram_style": "图片生成方式",
            "writing_guidelines": "话术表达风格",
        }

    @staticmethod
    def _default_skill_field_examples() -> dict[str, list[str]]:
        return {
            "common_titles": ["现状与约束", "方案如何落到执行"],
            "title_style": ["现状与约束", "方案如何落到执行"],
            "text_style": ["先界定适用范围，再沿“现状 → 判断 → 动作 → 验证”逐层收束。"],
            "diagram_style": ["PlantUML 活动图：主流程纵向排列，跨角色动作放入对应泳道。"],
            "writing_guidelines": ["需要说明的是，目标对象只覆盖已经确认的适用范围。"],
        }

    @classmethod
    def _default_skill_example_document(cls, doc_type: str) -> str:
        return cls._fallback_skill_example_document(doc_type, [], "")

    @staticmethod
    def _fallback_skill_example_document(
        doc_type: str,
        source_headings: list[str],
        source_content: str,
    ) -> str:
        """构造足够长且随源标题句式变化的安全示例，供模型失败或结果过短时使用。"""
        joined_headings = "\n".join(source_headings)
        question_style = bool(
            re.search(r"[？?]|为何|为什么|如何|怎么", joined_headings)
        )
        colon_style = bool(re.search(r"[：:]", joined_headings))
        parallel_style = bool(re.search(r"[与及]", joined_headings))

        def heading(
            plain: str,
            detail: str,
            question: str,
            parallel: Optional[str] = None,
        ) -> str:
            if question_style:
                return question
            if colon_style:
                return f"{plain}：{detail}"
            if parallel_style and parallel:
                return parallel
            return plain

        main_title = (
            "共享评审空间：预约流程与协作边界优化方案"
            if colon_style
            else (
                "共享评审空间预约流程与协作边界优化方案"
                if parallel_style
                else "共享评审空间预约流程优化方案"
            )
        )
        background_heading = heading(
            "背景与问题",
            "一次冲突暴露出的状态断点",
            "为什么现有预约方式需要调整",
            "背景与问题界定",
        )
        scope_heading = heading(
            "目标与范围",
            "先明确要解决什么",
            "这次要解决什么，不解决什么",
            "目标与适用范围",
        )
        design_heading = heading(
            "方案设计",
            "让状态、责任与动作相互对应",
            "方案如何落到执行",
            "方案设计与角色分工",
        )
        flow_heading = heading(
            "核心流程",
            "从提出申请到完成释放",
            "一次预约如何走完整个流程",
            "申请流程与状态流转",
        )
        risk_heading = heading(
            "风险与保障",
            "异常不能重新回到人工猜测",
            "出现异常时如何保持边界清楚",
            "风险识别与异常保障",
        )
        validation_heading = heading(
            "验证与复盘",
            "用可观察结果收束判断",
            "怎样判断这套方案真正有效",
            "验证方式与后续复盘",
        )
        conclusion_heading = heading(
            "结论与后续",
            "把临时协调变成稳定机制",
            "最终要形成什么结果",
            "结论与后续行动",
        )
        connector = (
            "基于此"
            if "基于此" in source_content
            else ("因此" if "因此" in source_content else "随后")
        )
        boundary_lead = (
            "需要说明的是"
            if "需要说明的是" in source_content
            else "需要明确的是"
        )
        diagram = ""
        lower_content = source_content.lower()
        if "```plantuml" in lower_content or "@startuml" in lower_content:
            diagram = """

正文中的关系可按同样术语画成组件图，阅读顺序从申请角色进入状态服务，再到资源维护角色：

```plantuml
@startuml
left to right direction
actor 申请角色
component 状态服务
actor 维护角色
申请角色 --> 状态服务 : 提交与确认
状态服务 --> 维护角色 : 通知与复核
维护角色 --> 状态服务 : 更新可用状态
@enduml
```"""
        elif "```mermaid" in lower_content:
            diagram = """

正文中的状态变化使用同一组动作词表达，避免图中另造一套术语：

```mermaid
flowchart LR
    提交申请 --> 检查冲突
    检查冲突 --> 确认使用
    确认使用 --> 完成释放
```"""

        return f"""# {main_title}

## 摘要

本文围绕一个完全虚构的共享评审空间场景，讨论预约信息分散、资源状态不透明和异常处理依赖口头协调的问题。方案的重点不是增加审批，而是让每次申请都能回答三个问题：当前由谁使用、下一步由谁处理、完成后凭什么确认资源已经释放。

全文先界定问题和适用范围，再把目标拆成可观察状态，随后给出角色分工、核心流程、异常保障与验证方式。所有判断都落到动作和证据，不使用真实组织、项目或业务数据。

## {background_heading}

共享评审空间同时服务准备材料、集中讨论和结果确认等活动。现有做法只记录“有人预约”，却没有说明准备是否完成、临时变更是否被接收、使用结束后资源是否已经恢复。信息看似存在，真正执行时仍要逐人询问。

问题的核心不是缺少一张登记表，而是状态、动作和责任没有对应关系。申请角色关心能否使用，维护角色关心是否满足开放条件，后续使用者关心资源何时重新可用；如果这些问题混在一个备注框里，任何变更都会重新触发人工确认。

## {scope_heading}

本次优化只处理预约发起、冲突确认、使用准备、完成释放和异常复核。目标是让相关角色不依赖额外询问，也能从同一处判断当前状态、待办动作和完成证据。界面样式、空间硬件和人员排班不在本次方案范围内。

{boundary_lead}，范围约束不是附注，而是后续取舍的依据。凡是不能改变状态判断、责任归属或验证结果的信息，都不进入主流程；确需保留的补充说明放在对应动作之后，避免重要条件被长段背景淹没。

## {design_heading}

方案把一次预约拆成“申请、确认、准备、使用、释放、复核”几个连续状态。每个状态都绑定进入条件、责任角色、应执行动作和完成证据；只有证据满足要求，状态才向后流转。这样既能保持流程简洁，也能避免角色凭经验猜测。

角色分工遵循“谁产生信息，谁负责首次更新；谁消费结果，谁负责确认可用”的原则：

- 申请角色说明使用目的、期望范围和必要准备，并对变更及时更新。
- 维护角色检查冲突与开放条件，只对自己能够验证的状态作确认。
- 使用角色在开始前确认资源状态，在结束后提交释放结果和遗留事项。
- 复核角色只处理异常和争议，不重复参与每一次正常流转。

## {flow_heading}

流程从申请角色提交用途和范围开始。系统先检查同一时段是否存在冲突；没有冲突时进入准备状态，有冲突时返回可调整的条件，而不是只给出“失败”结果。申请角色据此修改范围或撤回请求，避免维护角色在多个沟通渠道间转述。

{connector}，准备完成后由使用角色确认接手。确认动作意味着必要材料、访问边界和现场状态已经可用，而不是简单点击按钮。使用结束后，使用角色提交释放结果；若仍有遗留事项，则同时标明影响范围和下一位处理角色，流程不会把“已结束”误写成“已恢复”。

## {risk_heading}

主要风险来自三类断点：状态被更新但相关角色没有接收、异常被记录却没有明确下一步、完成结果缺少可复核证据。对应保障也不应写成宽泛口号，而要直接嵌入流程。

- 关键状态变化只保留一个正式入口，其他渠道只发送提醒，不形成第二份事实。
- 异常记录必须同时包含影响范围、临时处理和下一位责任角色。
- 释放动作必须附带可观察结果；无法确认时回到复核状态，不直接标记完成。
- 长时间没有推进的事项进入待复核列表，由相关角色判断继续、调整或关闭。{diagram}

## {validation_heading}

验证分为流程可执行性和结果可判断性。前者关注相关角色能否只凭当前记录完成下一步，后者关注状态变化是否都有对应证据。试运行期间不追求覆盖所有例外，而是优先验证主流程是否连续、异常是否能回到明确责任人。

复盘时按“现象、判断、动作、结果”记录，不把意见数量当作效果。若某个节点仍需要反复口头确认，应先检查进入条件是否含糊；若不同角色对完成状态理解不一，应先修正证据定义，而不是继续增加提醒。

## {conclusion_heading}

这套方案把一次临时协调转化为可以被读取、执行和复核的状态链路。它保留必要的人为判断，但让判断发生在边界明确的位置；它减少重复询问，但不以隐藏异常为代价。

后续优化应继续围绕同一目标展开：让每位相关角色在进入流程时知道自己为什么接手、需要完成什么、完成后留下什么证据。只要这三个问题能够稳定回答，共享资源的协作就不再依赖某位熟悉情况的人持续兜底。"""

    def _log_creation_usage(
        self,
        model_name: str,
        prompt_text: str,
        response_text: str,
        latency_ms: int,
        status: str,
        error_msg: Optional[str] = None,
    ) -> None:
        """记录创作模型 token 用量，失败不影响主生成链路。"""
        try:
            from monitor.llm_tracker import estimate_tokens, log_llm_usage

            log_llm_usage(
                caller="creation",
                model_name=model_name,
                prompt_tokens=estimate_tokens(prompt_text),
                completion_tokens=estimate_tokens(response_text),
                latency_ms=latency_ms,
                status=status,
                error_msg=error_msg,
                raw_preview=prompt_text,
                response_preview=response_text,
                db_path=self.db_path,
            )
        except Exception as exc:
            logger.warning("创作 token 用量埋点失败: %s", exc)

    async def _generate_cloud(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        api_key: str,
        base_url: str,
    ):
        is_claude = "claude" in model.lower() or "anthropic.com" in base_url
        if is_claude:
            url = self._anthropic_messages_url(base_url)
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            payload = {"model": model, "max_tokens": 8192, "stream": True, "system": system_prompt,
                       "messages": [{"role": "user", "content": user_message}]}
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    await self._raise_for_cloud_error(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            if data.get("type") == "content_block_delta":
                                text = data.get("delta", {}).get("text", "")
                                if text:
                                    yield text
                        except json.JSONDecodeError:
                            continue
        else:
            default_urls = {
                "gpt": "https://api.openai.com/v1",
                "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "glm": "https://open.bigmodel.cn/api/paas/v4",
                "moonshot": "https://api.moonshot.cn/v1",
            }
            if not base_url:
                for key, url in default_urls.items():
                    if key in model.lower():
                        base_url = url
                        break
                else:
                    base_url = "https://api.openai.com/v1"
            url = base_url.rstrip('/') + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "stream": True,
                       "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]}
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    await self._raise_for_cloud_error(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue

    async def _chat_cloud(self, messages: list, model: str, api_key: str, base_url: str):
        """多轮对话，供体验功能使用。"""
        is_claude = "claude" in model.lower() or "anthropic.com" in base_url
        if is_claude:
            url = self._anthropic_messages_url(base_url)
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            system, chat_msgs = self._normalize_anthropic_messages(messages)
            payload = {"model": model, "max_tokens": 2048, "stream": True, "system": system, "messages": chat_msgs}
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    await self._raise_for_cloud_error(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]": break
                        try:
                            d = json.loads(data_str)
                            if d.get("type") == "content_block_delta":
                                text = d.get("delta", {}).get("text", "")
                                if text: yield text
                        except json.JSONDecodeError: continue
        else:
            default_urls = {"gpt": "https://api.openai.com/v1", "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1", "glm": "https://open.bigmodel.cn/api/paas/v4", "moonshot": "https://api.moonshot.cn/v1"}
            if not base_url:
                for key, url in default_urls.items():
                    if key in model.lower(): base_url = url; break
                else: base_url = "https://api.openai.com/v1"
            url = base_url.rstrip('/') + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "stream": True, "messages": messages}
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    await self._raise_for_cloud_error(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]": break
                        try:
                            d = json.loads(data_str)
                            content = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content: yield content
                        except json.JSONDecodeError: continue

    @staticmethod
    def _anthropic_messages_url(base_url: str) -> str:
        """兼容用户填写根地址、/v1 或 /v1/messages 的 Anthropic API URL。"""
        url = (base_url or ANTHROPIC_DEFAULT_BASE_URL).strip().rstrip("/")
        if url.endswith("/v1/messages"):
            return url
        if url.endswith("/v1"):
            return f"{url}/messages"
        return f"{url}/v1/messages"

    @staticmethod
    def _normalize_anthropic_messages(messages: list) -> tuple[str, list[dict]]:
        """把体验对话清洗成 Anthropic Messages API 接受的结构。"""
        system_parts: list[str] = []
        chat_msgs: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                continue
            if chat_msgs and chat_msgs[-1]["role"] == role:
                chat_msgs[-1]["content"] += f"\n\n{content}"
            else:
                chat_msgs.append({"role": role, "content": content})

        if not chat_msgs:
            chat_msgs.append({"role": "user", "content": "Hello"})
        if chat_msgs[0]["role"] != "user":
            chat_msgs.insert(0, {"role": "user", "content": "Continue the conversation."})

        system = "\n\n".join(system_parts) or "You are a helpful assistant."
        return system, chat_msgs

    @staticmethod
    async def _raise_for_cloud_error(resp: httpx.Response) -> None:
        """把云模型服务返回的 JSON 错误转换为前端可读的异常文本。"""
        if resp.status_code < 400:
            return

        body = await resp.aread()
        detail = body.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(detail) if detail else {}
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("type") or detail
                else:
                    detail = parsed.get("detail") or parsed.get("message") or detail
        except json.JSONDecodeError:
            pass

        raise RuntimeError(f"模型请求失败 ({resp.status_code}): {detail or resp.reason_phrase}")

    def analyze_requirement(self, user_prompt: str, options: CreationOptions) -> dict:
        """轻量需求解析，先用规则把创作任务结构化。"""
        text = user_prompt.strip()
        doc_type = options.doc_type.strip() or self._infer_doc_type(text)
        audience = options.audience.strip() or self._infer_audience(text)
        keywords = self._extract_keywords(text)

        return {
            "topic": self._infer_topic(text),
            "doc_type": doc_type,
            "audience": audience,
            "keywords": keywords,
            "style": self._infer_style(text),
            "needs_latest": any(word in text for word in ["最新", "政策", "趋势", "行业", "联网", "互联网"]),
            "needs_images": options.enable_image_generation
            or any(word in text for word in ["图片", "配图", "架构图", "流程图", "插图", "封面图"]),
        }

    def retrieve_references(
        self,
        user_prompt: str,
        parsed_requirement: dict,
        options: CreationOptions,
    ) -> list[ReferenceDocument]:
        """多路召回：关键词召回 + 向量召回，融合排序。"""
        db = Path(self.db_path)
        if not db.exists():
            logger.warning("知识库数据库不存在: %s", db)
            return []

        # 路径1: 关键词召回
        try:
            keyword_rows = self._query_document_rows(user_prompt, parsed_requirement, options)
        except Exception as exc:
            logger.warning("关键词召回失败: %s", exc)
            keyword_rows = []

        # 路径2: 向量召回
        vector_rows = []
        if self.enable_vector_recall and self._embedding_model:
            try:
                vector_rows = self._vector_recall(user_prompt, options.max_references * 2)
            except Exception as exc:
                logger.warning("向量召回失败: %s", exc)

        # 合并去重。向量相似度必须保留下来参与统一评分；否则同一文档先被
        # 关键词路径命中时，后到的向量证据会在去重时被静默丢弃。
        merged_by_id: dict[int, dict] = {}
        for row in keyword_rows + vector_rows:
            doc_id = int(row.get("id") or 0)
            if not doc_id:
                continue
            candidate = dict(row)
            existing = merged_by_id.get(doc_id)
            if existing is None:
                merged_by_id[doc_id] = candidate
                continue
            existing["_vector_similarity"] = max(
                float(existing.get("_vector_similarity") or 0),
                float(candidate.get("_vector_similarity") or 0),
            )
        merged_rows = list(merged_by_id.values())

        if not merged_rows:
            return []

        # 统一评分
        max_usage = max(int(row.get("usage_count") or 0) for row in merged_rows) or 1
        now_ms = int(time.time() * 1000)
        refs: list[ReferenceDocument] = []
        for row in merged_rows:
            relevance = self._score_relevance(row, parsed_requirement)
            quality = self._score_quality(row)
            completeness = self._score_completeness(row)
            usage = math.log1p(int(row.get("usage_count") or 0)) / math.log1p(max_usage)
            format_score = self._score_format(row, parsed_requirement)
            freshness = self._score_freshness(int(row.get("updated_at") or 0), now_ms)

            final = (
                relevance * options.content_weight
                + quality * options.quality_weight
                + completeness * options.completeness_weight
                + usage * options.usage_weight
                + format_score * options.format_weight
                + freshness * options.freshness_weight
            )

            # 宁缺毋滥：相关性低于阈值直接丢弃
            if relevance < 0.25 or (relevance < 0.4 and final < 0.6):
                continue

            refs.append(
                ReferenceDocument(
                    id=int(row["id"]),
                    title=row.get("title") or "",
                    doc_type=row.get("doc_type") or "",
                    summary=row.get("summary") or "",
                    full_content=row.get("full_content") or "",
                    sections_json=row.get("sections_json") or "[]",
                    style_phrases=row.get("style_phrases") or "[]",
                    prompt_hint=row.get("prompt_hint") or "",
                    usage_count=int(row.get("usage_count") or 0),
                    review_status=row.get("review_status") or "",
                    updated_at=int(row.get("updated_at") or 0),
                    source_url=row.get("source_url"),
                    relevance_score=relevance,
                    quality_score=quality,
                    completeness_score=completeness,
                    usage_score=usage,
                    format_score=format_score,
                    freshness_score=freshness,
                    final_weight=final,
                    reason=self._build_reason(relevance, quality, completeness, usage, format_score),
                )
            )

        refs.sort(key=lambda item: item.final_weight, reverse=True)
        return refs[: max(1, min(options.max_references, 30))]

    async def collect_web_context(
        self,
        user_prompt: str,
        parsed_requirement: dict,
    ) -> list[WebSearchResult]:
        """执行轻量互联网检索。无专用搜索 API 时使用 DuckDuckGo HTML 降级。"""
        queries = self._build_search_queries(user_prompt, parsed_requirement)
        results: list[WebSearchResult] = []
        for query in queries[:3]:
            try:
                results.extend(await self._search_duckduckgo(query))
            except Exception as exc:
                logger.warning("互联网检索失败 query=%s error=%s", query, exc)

        deduped: list[WebSearchResult] = []
        seen: set[str] = set()
        for item in results:
            key = item.url or item.title
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:6]

    async def search_github_context(
        self,
        user_prompt: str,
        parsed_requirement: dict,
    ) -> list[GithubSearchResult]:
        """检索公开 GitHub 仓库；不接收、不读取也不记录用户 Token。"""
        keywords = [
            str(item).strip()
            for item in (parsed_requirement.get("keywords") or [])
            if str(item).strip()
        ]
        topic = str(parsed_requirement.get("topic") or user_prompt).strip()
        query = " ".join([*keywords[:5], topic[:80]]).strip()[:220]
        if not query:
            return []

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "MemoryBreadCreation/1.0",
        }
        try:
            async with httpx.AsyncClient(
                timeout=12.0,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(
                    "https://api.github.com/search/repositories",
                    params={
                        "q": query,
                        "sort": "stars",
                        "order": "desc",
                        "per_page": 5,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("GitHub 公开仓库检索失败: %s", type(exc).__name__)
            return []

        results: list[GithubSearchResult] = []
        for item in payload.get("items", [])[:5]:
            full_name = str(item.get("full_name") or "").strip()
            url = str(item.get("html_url") or "").strip()
            if not full_name or not self._is_reasonable_web_url(url):
                continue
            results.append(
                GithubSearchResult(
                    full_name=full_name,
                    url=url,
                    description=self._clip(str(item.get("description") or ""), 320),
                    stars=max(0, int(item.get("stargazers_count") or 0)),
                    language=str(item.get("language") or ""),
                    updated_at=str(item.get("updated_at") or ""),
                )
            )
        return results

    def _query_document_rows(
        self,
        user_prompt: str,
        parsed_requirement: dict,
        options: CreationOptions,
    ) -> list[dict]:
        keywords = parsed_requirement.get("keywords") or []
        like_terms = keywords[:8] or [user_prompt[:24]]
        params: list[object] = []
        clauses: list[str] = ["deleted_at IS NULL"]

        if parsed_requirement.get("doc_type"):
            clauses.append("(doc_type = ? OR title LIKE ? OR COALESCE(prompt_hint, '') LIKE ?)")
            doc_type = parsed_requirement["doc_type"]
            params.extend([doc_type, f"%{doc_type}%", f"%{doc_type}%"])

        keyword_clauses = []
        for term in like_terms:
            pattern = f"%{term}%"
            keyword_clauses.append(
                "CASE WHEN (title LIKE ? OR COALESCE(summary, '') LIKE ? OR "
                "COALESCE(full_content, '') LIKE ?) THEN 1 ELSE 0 END"
            )
            params.extend([pattern] * 3)
        if keyword_clauses:
            min_matches = max(1, len(like_terms) // 2)  # 至少匹配一半关键词
            clauses.append(f"({' + '.join(keyword_clauses)}) >= {min_matches}")

        sql = f"""
            SELECT id, title, doc_type, summary, full_content, sections_json, style_phrases,
                   prompt_hint, usage_count, review_status, updated_at, source_url
            FROM bake_documents
            WHERE {' AND '.join(clauses)}
            ORDER BY usage_count DESC, updated_at DESC, id DESC
            LIMIT ?
        """
        params.append(max(options.max_references * 4, 16))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            if rows:
                return rows
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, title, doc_type, summary, full_content, sections_json, style_phrases,
                           prompt_hint, usage_count, review_status, updated_at, source_url
                    FROM bake_documents
                    WHERE deleted_at IS NULL
                    ORDER BY usage_count DESC, updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (max(options.max_references * 3, 12),),
                ).fetchall()
            ]
        finally:
            conn.close()

    def _vector_recall(self, query: str, limit: int = 10) -> list[dict]:
        """向量召回：通过embedding相似度召回文档。"""
        if not self._embedding_model:
            return []

        # 生成query向量
        try:
            query_emb = self._embedding_model.encode([query])[0]
            query_vector = query_emb.vector
        except Exception as e:
            logger.error("生成query向量失败: %s", e)
            return []

        # 从数据库加载所有文档及其内容
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, title, doc_type, summary, full_content, sections_json, style_phrases,
                       prompt_hint, usage_count, review_status, updated_at, source_url
                FROM bake_documents
                WHERE deleted_at IS NULL AND full_content IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 100
                """
            ).fetchall()

            if not rows:
                return []

            # 计算相似度
            scored_docs = []
            for row in rows:
                text = (row["summary"] or "") + "\n" + (row["full_content"] or "")[:500]
                if not text.strip():
                    continue
                try:
                    doc_emb = self._embedding_model.encode([text])[0]
                    doc_vector = doc_emb.vector
                    similarity = self._cosine_similarity(query_vector, doc_vector)
                    if similarity > 0.5:  # 相似度阈值
                        scored_docs.append((dict(row), similarity))
                except Exception as e:
                    logger.debug("计算文档 %s 向量失败: %s", row["id"], e)
                    continue

            # 按相似度排序
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            logger.info("向量召回: %d个文档（相似度>0.5）", len(scored_docs))
            recalled = []
            for doc, score in scored_docs[:limit]:
                item = dict(doc)
                item["_vector_similarity"] = float(score)
                recalled.append(item)
            return recalled

        finally:
            conn.close()

    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """计算余弦相似度。"""
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)

    def _build_search_queries(self, user_prompt: str, parsed_requirement: dict) -> list[str]:
        topic = parsed_requirement.get("topic") or user_prompt
        doc_type = parsed_requirement.get("doc_type") or ""
        keywords = " ".join((parsed_requirement.get("keywords") or [])[:4])
        base = " ".join(part for part in [topic, doc_type, keywords] if part)
        return [
            f"{base} 最新政策 标准",
            f"{base} 行业方案 案例",
            f"{base} 技术架构 最佳实践",
        ]

    async def _search_duckduckgo(self, query: str) -> list[WebSearchResult]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {"User-Agent": "Mozilla/5.0 MemoryBreadCreation/1.0"}
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()

        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
            re.S,
        )
        results = []
        for match in pattern.finditer(response.text):
            title = self._strip_html(match.group("title"))
            snippet = self._strip_html(match.group("snippet"))
            result_url = match.group("url")
            if result_url.startswith("//"):
                result_url = "https:" + result_url
            elif result_url.startswith("/"):
                result_url = "https://duckduckgo.com" + result_url
            if title and self._is_reasonable_web_url(result_url):
                results.append(WebSearchResult(title=title, url=result_url, snippet=snippet))
        return results[:4]

    def _build_system_prompt(self, design_templates: list[dict], options: CreationOptions) -> str:
        template_hint = ""
        if design_templates:
            names = "、".join(
                str(item.get("name") or item.get("title") or "未命名模板")
                for item in design_templates[:5]
            )
            template_hint = f"\n可参考的文档模板：{names}"

        return f"""你是一个专业的企业文档创作助手，擅长基于历史文档、知识库和操作手册生成新文档。

工作原则：
1. 使用 Markdown 输出，内容直接可用，不输出思考过程。
2. 优先使用高权重参考资料中的事实、结构、术语和格式风格。
3. 如果参考资料不足，可以生成合理的增量内容，但需要避免编造具体数据、政策编号、客户名称。
4. 若启用互联网检索，请基于检索摘要补充内容，并对政策、标准、日期、数字保留核验说明。
5. 若启用图片生成，请在合适章节插入图片建议占位符，格式为：[图片建议：用途 | 画面/图示要求]。
6. 技术架构图、流程图、关系图优先给出 Mermaid 图，不用纯文字替代。
7. 章节结构要完整，标题层级清晰，语言正式、克制、专业。
8. 根据用户输入的语言进行回复。如果用户使用中文提问，全文必须使用中文输出；如果用户使用英文提问，全文必须使用英文输出。
9. 【强制要求】在正文中每次引用参考资料内容时，必须在引用处插入 Markdown 内联链接，格式严格为 [引用说明](#ref-数字ID)，数字ID 来自参考资料标注的 ref-id。示例：[参见分销诊断框架](#ref-42)。若未插入此格式的引用链接，视为未完成任务。
10. 禁止输出原生 HTML 标签或空锚点，例如 <a id="..."></a>。标题锚点只使用 Markdown 链接引用已有标题。
{template_hint}

输出格式偏好：{options.output_format}"""

    def _build_user_message(
        self,
        user_prompt: str,
        timeline_context: Optional[str],
        capture_context: Optional[str],
        options: CreationOptions,
        parsed_requirement: dict,
        references: list[ReferenceDocument],
        web_results: list[WebSearchResult],
    ) -> str:
        blocks = [
            "请根据以下创作任务生成完整文档。",
            "",
            "【本次创作需求】",
            user_prompt,
            "",
            "【解析后的任务画像】",
            json.dumps(parsed_requirement, ensure_ascii=False, indent=2),
            "",
            "【生成控制】",
            f"- 继承历史格式：{'是' if options.inherit_format else '否'}",
            f"- 启用 RAG：{'是' if options.enable_rag else '否'}",
            f"- 需要互联网检索补充：{'是' if options.enable_web_search else '否'}",
            f"- 需要图片/图示：{'是' if options.enable_image_generation else '否'}",
        ]

        if timeline_context:
            blocks.extend(["", "【参考时间线】", timeline_context])
        if capture_context:
            blocks.extend(["", "【参考采集记录】", capture_context])

        if references:
            blocks.extend(["", "【高权重参考资料】"])
            for index, ref in enumerate(references, 1):
                content = self._clip(self._best_reference_content(ref), 1200)
                sections = self._clip(self._safe_json_summary(ref.sections_json), 500)
                style = self._clip(self._safe_json_summary(ref.style_phrases), 260)
                blocks.extend(
                    [
                        f"### R{index}. {ref.title} (ref-id: {ref.id})",
                        f"- 文档类型：{ref.doc_type or '未知'}",
                        f"- 综合权重：{ref.final_weight:.2f}",
                        f"- 推荐原因：{ref.reason}",
                        f"- 使用热度：{ref.usage_count}",
                        f"- 结构/格式线索：{sections or '无'}",
                        f"- 风格线索：{style or '无'}",
                        f"- 内容摘录：\n{content}",
                    ]
                )
        else:
            blocks.extend(["", "【高权重参考资料】", "未召回到可用参考资料，请根据需求生成合理增量内容。"])

        if web_results:
            blocks.extend(["", "【互联网检索结果】"])
            for index, item in enumerate(web_results, 1):
                blocks.extend(
                    [
                        f"W{index}. {item.title}",
                        f"- URL：{item.url}",
                        f"- 摘要：{self._clip(item.snippet, 260)}",
                    ]
                )
            blocks.append("- 使用规则：外部资料只作为补充参考；涉及政策、标准、日期、数字的内容需保留核验项。")
        elif options.enable_web_search or parsed_requirement.get("needs_latest"):
            blocks.extend(
                [
                    "",
                    "【互联网检索要求】",
                    "- 请列出建议检索的问题或关键词。",
                    "- 对涉及最新政策、标准、价格、版本、日期的信息，用'待联网核验'标记，不要编造具体来源。",
                ]
            )

        if options.enable_image_generation or parsed_requirement.get("needs_images"):
            blocks.extend(
                [
                    "",
                    "【图片与图示要求】",
                    "- 在需要配图的位置插入图片建议占位符。",
                    "- 对流程、架构、关系类内容，优先输出 Mermaid 图。",
                    "- 对封面、场景、宣传类图片，写出可直接给生图模型使用的中文 prompt。",
                ]
            )

        blocks.extend(
            [
                "",
                "【输出要求】",
                "1. 先输出标题和简短摘要。",
                "2. 再输出目录式章节正文，章节不少于 5 个。",
                "3. 在正文中引用参考资料时，必须在引用位置插入内联链接，格式为 [引用说明](#ref-{id})，其中 {id} 替换为对应参考资料括号内的 ref-id 数字。例如参考资料标注 (ref-id: 42)，则引用写作 [见参考方案](#ref-42)。",
                "4. 需要包含'参考资料使用说明'，列出哪些内容/格式来自高权重参考。",
                "5. 需要包含'后续核验与补充清单'，列出联网检索、图片生成或人工审核事项。",
                "6. 不要输出任何原生 HTML 标签；尤其不要用 <a id=\"...\"></a> 为章节添加锚点。",
                "7. 直接开始输出文档正文。",
            ]
        )

        return "\n".join(blocks)

    def _build_qwen35_prompt(self, system_prompt: str, user_message: str) -> str:
        """构建 Qwen3.5 的 raw 模式 prompt，使用官方 chat template。"""
        return (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_message}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

    async def _stream_qwen35_raw(self, response):
        """解析 Qwen3.5 raw 模式的流式响应，过滤 <think> 标签内的内容。"""
        import re
        in_think = False
        think_buffer = ""
        async for line in response.aiter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = data.get("response", "")
            if not text:
                continue
            # 逐字符处理，过滤 <think>...</think> 块
            output = ""
            for ch in text:
                if not in_think:
                    if ch == "<":
                        think_buffer = ch
                    elif think_buffer:
                        think_buffer += ch
                        if think_buffer == "<think>":
                            in_think = True
                            think_buffer = ""
                        elif not "<think>".startswith(think_buffer):
                            output += think_buffer
                            think_buffer = ""
                    else:
                        output += ch
                else:
                    if ch == ">":
                        think_buffer += ch
                        if think_buffer.endswith("</think>"):
                            in_think = False
                            think_buffer = ""
                    else:
                        think_buffer += ch
            if output:
                yield output

    def _infer_doc_type(self, text: str) -> str:
        mapping = [
            ("操作手册", ["操作手册", "使用手册", "用户手册", "SOP", "流程"]),
            ("建设方案", ["建设方案", "实施方案", "总体方案", "解决方案", "技术方案"]),
            ("汇报材料", ["汇报", "述职", "总结", "报告"]),
            ("制度文档", ["制度", "规范", "管理办法", "规定"]),
            ("产品方案", ["产品方案", "需求文档", "PRD", "MRD"]),
            ("指南", ["指南"]),
        ]
        for doc_type, words in mapping:
            if any(word in text for word in words):
                return doc_type
        return "通用文档"

    def _infer_audience(self, text: str) -> str:
        for audience in ["政府", "企业管理人员", "客户", "研发团队", "运维人员", "销售", "领导"]:
            if audience in text:
                return audience
        return "业务与技术相关人员"

    def _infer_topic(self, text: str) -> str:
        match = re.search(r'[《“""]([^》”""]{2,60})[》”""]', text)
        if match:
            return match.group(1)
        compact = re.sub(r"\s+", "", text)
        return compact[:50] or "未命名主题"

    def _infer_style(self, text: str) -> str:
        if any(word in text for word in ["正式", "严谨", "政务", "汇报"]):
            return "正式严谨"
        if any(word in text for word in ["简洁", "短", "提纲"]):
            return "简洁提纲"
        if any(word in text for word in ["手册", "步骤", "操作"]):
            return "步骤化说明"
        return "专业清晰"

    def _extract_keywords(self, text: str) -> list[str]:
        try:
            import jieba
            tokens = list(jieba.cut(text))
        except (ImportError, Exception):
            # 无 jieba 时使用可预测的中文片段和二元词，避免把
            # “写一份周年员工礼物指南”切成无法命中文档的畸形长词。
            text_clean = re.sub(
                r"(?:请|帮我|帮忙|给我|写一份|生成一份|生成|撰写|输出|制作)",
                " ",
                text,
            )
            segments = [
                item
                for item in re.split(r"[\s，。；：、,.!?！？的了是在]+", text_clean)
                if len(item) >= 2
            ]
            tokens = []
            for segment in segments:
                tokens.append(segment)
                if len(segment) > 2:
                    tokens.extend(
                        segment[index:index + 2]
                        for index in range(len(segment) - 1)
                    )

        stop = {"帮我", "生成", "一份", "关于", "根据", "参考", "文档", "内容", "格式", "需要", "本次"}
        seen: set[str] = set()
        result: list[str] = []
        for token in tokens:
            if token in stop or len(token) < 2 or token in seen:
                continue
            seen.add(token)
            result.append(token)
        return result[:12]

    def _score_relevance(self, row: dict, parsed_requirement: dict) -> float:
        vector_similarity = min(
            max(float(row.get("_vector_similarity") or 0), 0.0),
            1.0,
        )
        haystack = "\n".join(
            str(row.get(key) or "")
            for key in ["title", "doc_type", "summary", "full_content", "sections_json", "prompt_hint"]
        )
        keywords = parsed_requirement.get("keywords") or []
        if not keywords:
            return max(0.35, vector_similarity)
        hits = sum(1 for word in keywords if word and word in haystack)
        title_hits = sum(1 for word in keywords if word and word in str(row.get("title") or ""))
        score = (hits / max(len(keywords), 1)) + min(title_hits, 3) * 0.12
        if score < 0.4:
            score = 0.0
        if parsed_requirement.get("doc_type") and parsed_requirement["doc_type"] == row.get("doc_type"):
            score += 0.15
        # 向量召回本身就是独立的相关性证据，不能再被词面分词结果清零。
        return min(max(score, vector_similarity), 1.0)

    def _score_quality(self, row: dict) -> float:
        status = str(row.get("review_status") or "")
        base = 0.55
        if status in {"adopted", "auto_created", "verified", "enabled"}:
            base += 0.25
        if row.get("summary"):
            base += 0.08
        if row.get("prompt_hint"):
            base += 0.06
        if row.get("full_content"):
            base += 0.06
        return min(base, 1.0)

    def _score_completeness(self, row: dict) -> float:
        content_len = len(str(row.get("full_content") or ""))
        sections = self._json_len(row.get("sections_json"))
        section_score = min(sections / 6, 1.0)
        content_score = min(content_len / 3000, 1.0)
        return max(0.25, section_score * 0.55 + content_score * 0.45)

    def _score_format(self, row: dict, parsed_requirement: dict) -> float:
        sections = self._json_len(row.get("sections_json"))
        styles = self._json_len(row.get("style_phrases"))
        score = min(sections / 6, 1.0) * 0.7 + min(styles / 6, 1.0) * 0.3
        if parsed_requirement.get("doc_type") and parsed_requirement["doc_type"] == row.get("doc_type"):
            score += 0.12
        return min(max(score, 0.2), 1.0)

    def _score_freshness(self, updated_at: int, now_ms: int) -> float:
        if updated_at <= 0:
            return 0.35
        age_days = max((now_ms - updated_at) / 86_400_000, 0)
        return max(0.25, 1.0 - min(age_days / 365, 0.75))

    def _build_reason(
        self,
        relevance: float,
        quality: float,
        completeness: float,
        usage: float,
        format_score: float,
    ) -> str:
        reasons = []
        if relevance >= 0.65:
            reasons.append("主题高度相关")
        elif relevance >= 0.35:
            reasons.append("主题部分相关")
        if quality >= 0.8:
            reasons.append("质量较高")
        if completeness >= 0.7:
            reasons.append("结构/内容完整")
        if usage >= 0.6:
            reasons.append("历史使用较多")
        if format_score >= 0.7:
            reasons.append("格式可继承")
        return "，".join(reasons) or "可作为补充参考"

    def _best_reference_content(self, ref: ReferenceDocument) -> str:
        return ref.full_content or ref.summary or ref.prompt_hint

    def _clip(self, text: str, limit: int) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def _json_len(self, raw: object) -> int:
        try:
            value = json.loads(raw or "[]")
        except Exception:
            return 0
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
        return 0

    def _safe_json_summary(self, raw: str) -> str:
        try:
            value = json.loads(raw or "[]")
        except Exception:
            return str(raw or "")
        if isinstance(value, list):
            return "；".join(str(item) for item in value[:8])
        if isinstance(value, dict):
            return "；".join(f"{key}: {value[key]}" for key in list(value.keys())[:8])
        return str(value)

    def _strip_html(self, value: str) -> str:
        value = re.sub(r"<.*?>", "", value or "")
        value = (
            value.replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&#x27;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        return re.sub(r"\s+", " ", value).strip()

    def _is_reasonable_web_url(self, value: str) -> bool:
        try:
            parsed = urlparse(value)
        except Exception:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
