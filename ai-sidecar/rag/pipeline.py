"""
RagPipeline — 完整 RAG 查询流水线

流程：
  Query → Embedding → [Qdrant 语义 + FTS5 关键词] → RRF 合并 → Prompt 组装 → LLM 推理 → 结果
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Callable, Optional

from embedding.model import EmbeddingModel

from .llm.base import LlmBackend
from .reranker import reciprocal_rank_fusion
from .retriever import (
    Fts5Retriever,
    KnowledgeFts5Retriever,
    RetrievedChunk,
    VectorRetriever,
    VectorSearchFilter,
    _document_url_doc_key,
)

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = (
    "你是记忆面包，一个本地运行的 AI 工作助手。"
    "根据以下工作记录上下文，简洁、准确地回答用户的问题，不要向用户反问或要求补充信息。"
    "如果上下文中没有相关信息，请直接说明。\n"
    "当用户要求周报、日报、项目总结等工作产出时，请按清晰的报告结构输出，只基于参考资料展开，"
    "没有数据支撑的章节直接跳过，不输出占位文字。\n"
    "涉及 OKR/KPI/量化进展时，量化结论必须有参考资料中的证据支撑，无证据不得编造数字。\n"
    "当用户使用「本周/上周/今天/昨天/最近」等相对时间时，以上下文中给出的【时间口径】为准，"
    "并结合每条记录的时间标注（看到时间/事件时间/创建或更新时间）判断其是否落在所请求的周期内；"
    "落在周期内的记录视为有效证据，不得以时间定义不明确为由拒绝总结。\n"
    "回答中提到文档或网页时，若参考资料已给出该文档的 URL，必须以 Markdown 超链接格式"
    "[文档名](URL) 一并给出；没有 URL 的只提名称，不得编造链接。"
)

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _format_date_cn(ts_ms: Optional[int]) -> str:
    """把毫秒时间戳格式化为『YYYY-MM-DD（周X）』的本地日期文本。"""
    if not ts_ms:
        return ""
    try:
        local = time.localtime(ts_ms / 1000)
        return f"{time.strftime('%Y-%m-%d', local)}（{_WEEKDAY_NAMES[local.tm_wday]}）"
    except Exception:
        return str(ts_ms)


def _iso_week_of(ts_ms: int) -> tuple:
    try:
        local = time.localtime(ts_ms / 1000)
        iso = _date(local.tm_year, local.tm_mon, local.tm_mday).isocalendar()
        return int(iso[0]), int(iso[1])
    except Exception:
        return 0, 0


def _build_relative_time_clause(intent: "QueryIntent") -> str:
    """为含相对时间的查询注入确定性时间口径。

    模型看不到系统时钟，若只拿到「本周」这类词，会因无法解析起止日期而
    拒绝采信周期内的记录。这里把解析结果显式写入 prompt，并提示模型用
    每条记录的时间标注（看到时间/事件时间/创建或更新时间）佐证归属。
    """
    if not intent.period_kind or not intent.period_display:
        return ""
    today = _format_date_cn(int(time.time() * 1000))
    return (
        f"【时间口径】今天是 {today}，用户提到的「{intent.period_phrase}」指 "
        f"{intent.period_display}。下方每条工作记录都带有时间标注（看到时间/事件时间/"
        "创建或更新时间），请以这些时间判断记录是否落在该周期内；周期内的记录"
        "可正常作为事实证据，不得以时间定义不明确为由拒绝总结或要求用户补充时间。\n\n"
    )


def _extract_core_retrieval_query(user_query: str) -> str:
    """For structured assistant prompts, keep retrieval focused on the actual user question."""
    text = (user_query or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("检索问题：") or line.startswith("检索问题:"):
            return line.split(":", 1)[1].strip() if ":" in line else line.split("：", 1)[1].strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("核心问题：") or line.startswith("核心问题:"):
            return line.split(":", 1)[1].strip() if ":" in line else line.split("：", 1)[1].strip()
    for marker in ("用户手工指令：", "用户手工指令:"):
        if marker not in text:
            continue
        section = text.split(marker, 1)[1]
        for next_marker in (
            "\n当前屏幕 OCR：",
            "\n当前屏幕 OCR:",
            "\n用户随本次请求附加了以下文件。",
        ):
            if next_marker in section:
                section = section.split(next_marker, 1)[0]
        manual_instruction = section.strip()
        if manual_instruction:
            return manual_instruction
    return text

_MAX_CHUNK_LEN = 800   # 单个上下文片段最大字符数
_KEYWORD_RRF_WEIGHT = 0.45
_PENDING_DOCUMENT_RRF_WEIGHT = 0.7
_VECTOR_RRF_WEIGHT = 1.0
# 关键词知识首位在 0.45 权重、RRF k=60 时的分数约为 0.00738。
# 阈值高于该值会在向量召回存在时误删直接命中的持久知识，只留下向量结果。
_LOOKUP_MIN_RRF_SCORE_WITH_VECTOR = 0.01
_VECTOR_SCORE_THRESHOLD = 0.45


@dataclass
class RagResult:
    """RAG 查询结果"""

    answer: str
    contexts: list[RetrievedChunk] = field(default_factory=list)
    model: str = ""
    tokens: int = 0
    done_reason: Optional[str] = None
    output_truncated: bool = False


def _is_output_truncated(done_reason: Optional[str]) -> bool:
    reason = str(done_reason or "").lower()
    if not reason:
        return False
    return (
        reason == "length"
        or "max_tokens" in reason
        or "max_output" in reason
        or "token_limit" in reason
    )


@dataclass
class QueryIntent:
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None
    observed_start_ts: Optional[int] = None
    observed_end_ts: Optional[int] = None
    event_start_ts: Optional[int] = None
    event_end_ts: Optional[int] = None
    entity_terms: list[str] = field(default_factory=list)
    app_names: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    category: Optional[str] = None
    target_time_semantics: str = "either"
    activity_types: list[str] = field(default_factory=list)
    content_origins: list[str] = field(default_factory=list)
    history_view: Optional[bool] = None
    is_self_generated: Optional[bool] = None
    evidence_strengths: list[str] = field(default_factory=list)
    period_kind: str = ""
    period_phrase: str = ""
    period_display: str = ""


class RagPipeline:
    """
    RAG 流水线编排器。

    所有依赖（embedding_model, vector_retriever, fts5_retriever, llm）均通过
    构造函数注入，支持完整的 Mock 替换以便测试。
    """

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        vector_retriever: VectorRetriever,
        fts5_retriever: Fts5Retriever,
        llm: LlmBackend,
        knowledge_retriever: Optional[KnowledgeFts5Retriever] = None,
        top_k: int = 5,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        db_path: Optional[str] = None,
    ) -> None:
        self._embed = embedding_model
        self._vector = vector_retriever
        self._fts5 = fts5_retriever
        self._knowledge = knowledge_retriever
        self._llm = llm
        self._top_k = top_k
        self._system = system_prompt
        self._db_path = db_path

    def _read_user_identity(self) -> str:
        """从 user_preferences 表读取用户身份关键词"""
        if not self._db_path:
            return ""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM user_preferences WHERE key = 'user.identity_keywords' LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()
            return (row[0] or "").strip() if row else ""
        except Exception as exc:
            logger.warning("读取用户身份偏好失败: %s", exc)
            return ""

    def _baked_document_doc_keys(self) -> set[str]:
        """已烘焙文档的 doc_key 集合，用于让 pending 通道回归"未烘焙才兜底"语义。"""
        if not self._db_path:
            return set()
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bake_documents'"
            )
            if not cursor.fetchone():
                conn.close()
                return set()
            cursor.execute(
                "SELECT source_url FROM bake_documents "
                "WHERE deleted_at IS NULL AND source_url IS NOT NULL AND source_url != ''"
            )
            keys = {_document_url_doc_key(url) for (url,) in cursor.fetchall()}
            conn.close()
            keys.discard("")
            return keys
        except Exception as exc:
            logger.warning("查询已烘焙文档清单失败，跳过重复过滤: %s", exc)
            return set()

    def _build_identity_clause(self, user_identity: str) -> str:
        """生成用于注入到 system prompt 的身份说明段落"""
        if not user_identity:
            return ""
        names = [n.strip() for n in user_identity.split(",") if n.strip()]
        if not names:
            return ""
        names_str = "、".join(f'"{n}"' for n in names)
        return (
            f"\n\n【用户身份】屏幕的使用者是 {names_str}。"
            "在分析工作记录时，请注意：\n"
            "- 如果记录中的工作内容是由该用户自己操作、输入或编写的，应作为用户本人的工作产出纳入报告\n"
            "- 如果记录显示的是他人（非该用户）的工作内容，应酌情降低重要性或在描述中注明「用户在查看他人的…」\n"
            "- 无法判断时，按正常流程处理"
        )

    def query(
        self,
        user_query: str,
        top_k: Optional[int] = None,
        llm=None,
        references_only: bool = False,
        on_contexts: Optional[Callable[[list[RetrievedChunk]], None]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> RagResult:
        """执行完整 RAG 查询，返回 LLM 答案及引用的上下文片段。"""
        retrieval_query = _extract_core_retrieval_query(user_query)
        effective_top_k = max(1, int(top_k or self._top_k))
        # 意图只从真正的检索问题中提取。悬浮咨询会在原始请求里附加
        # “核心问题/用户问题理解/输出格式”等包装文本，这些内容不能改变召回策略。
        intent = self._parse_query_intent(retrieval_query)
        retrieval_started = time.perf_counter()

        query_vector: list[float] = []
        try:
            embed_results = self._embed.encode([retrieval_query])
            if embed_results:
                query_vector = embed_results[0].vector
        except Exception as exc:
            logger.warning("Query embedding 失败: %s", exc)
            # 向量只是多路召回之一；本地模型暂不可用时继续走持久知识/FTS，
            # 避免整个咨询链路因可选增强能力故障而不可用。
            query_vector = []
        embedding_finished = time.perf_counter()

        # 结构化查询（检索问题 ≠ 原始 query）时不附加实体词过滤，避免双重收窄
        knowledge_entity_terms = None if retrieval_query != user_query else (intent.entity_terms or None)

        logger.info(
            "统一召回: requested_top_k=%s start_ts=%s end_ts=%s",
            effective_top_k,
            intent.start_ts,
            intent.end_ts,
        )
        # 可以扩大内部候选池用于融合和重排，但最终上下文严格受调用方
        # requested_top_k 约束，不能由后端静默扩成 12/20 条。
        knowledge_top_k = max(effective_top_k * 6, 24)

        knowledge_results = self._knowledge.search(
            retrieval_query,
            top_k=knowledge_top_k,
            start_ts=intent.start_ts,
            end_ts=intent.end_ts,
            entity_terms=knowledge_entity_terms,
            observed_start_ts=intent.observed_start_ts,
            observed_end_ts=intent.observed_end_ts,
            event_start_ts=intent.event_start_ts,
            event_end_ts=intent.event_end_ts,
            activity_types=intent.activity_types or None,
            content_origins=intent.content_origins or None,
            history_view=intent.history_view,
            is_self_generated=intent.is_self_generated,
            evidence_strengths=intent.evidence_strengths or None,
        ) if self._knowledge else []
        knowledge_finished = time.perf_counter()

        logger.info(f"知识库检索结果: {len(knowledge_results)} 条")

        # 尚未烘焙完成的文档也应能通过原始 capture 全文命中。这里仅接纳带
        # 文档 URL 的结果，避免把普通屏幕噪声重新引入 RAG 上下文。
        pending_document_results: list[RetrievedChunk] = []
        raw_capture_results = self._fts5.search(
            retrieval_query,
            top_k=max(effective_top_k * 4, 12),
            start_ts=intent.observed_start_ts or intent.start_ts,
            end_ts=intent.observed_end_ts or intent.end_ts,
            entity_terms=knowledge_entity_terms,
        )
        pending_document_results = _build_pending_document_candidates(
            raw_capture_results,
            retrieval_query,
            limit=max(effective_top_k * 2, 6),
        )
        # 已有烘焙产物的 URL 不再走 capture 兜底：capture 的高 BM25 原始分会在
        # RRF 中抢占同 doc_key，用垃圾 AX 片段替换烘焙正文。
        baked_doc_keys = self._baked_document_doc_keys()
        skipped_baked = 0
        if baked_doc_keys:
            before = len(pending_document_results)
            pending_document_results = [
                chunk for chunk in pending_document_results
                if chunk.doc_key not in baked_doc_keys
            ]
            skipped_baked = before - len(pending_document_results)
        logger.info(
            "待烘焙文档检索结果: %s 条（排除已烘焙 URL %s 条）",
            len(pending_document_results),
            skipped_baked,
        )
        pending_document_finished = time.perf_counter()

        # 时间窗空结果兜底：查询含显式时间窗但无 knowledge 结果时，扩大到最近 14 天重试一次
        if intent.start_ts is not None and not knowledge_results:
            logger.info("时间窗内无 knowledge 数据，回退到最近 14 天")
            fallback_start = int(time.time() * 1000) - 14 * 24 * 60 * 60 * 1000
            knowledge_results = self._knowledge.search(
                retrieval_query,
                top_k=effective_top_k * 2,
                observed_start_ts=fallback_start,
                observed_end_ts=intent.observed_end_ts,
            ) if self._knowledge else []
        knowledge_fallback_finished = time.perf_counter()
        vector_results = (
            self._vector.search(
                query_vector,
                top_k=effective_top_k * 3,
                score_threshold=_VECTOR_SCORE_THRESHOLD,
                filters=VectorSearchFilter(
                    start_ts=intent.start_ts,
                    end_ts=intent.end_ts,
                    observed_start_ts=intent.observed_start_ts,
                    observed_end_ts=intent.observed_end_ts,
                    event_start_ts=intent.event_start_ts,
                    event_end_ts=intent.event_end_ts,
                    source_types=["knowledge", "document"],
                    # ASCII 实体不等于应用名（例如 AIGC、GPU）。应用过滤一旦
                    # 猜错会让整条向量通道归零，统一链路交给相关性重排处理。
                    app_names=None,
                    category=intent.category,
                    activity_types=intent.activity_types or None,
                    content_origins=intent.content_origins or None,
                    history_view=intent.history_view,
                    is_self_generated=intent.is_self_generated,
                    evidence_strengths=intent.evidence_strengths or None,
                ),
            )
            if query_vector else []
        )
        vector_finished = time.perf_counter()

        materialize_durable = getattr(
            type(self._knowledge),
            "materialize_durable_knowledge",
            None,
        )
        if callable(materialize_durable):
            knowledge_results = materialize_durable(
                self._knowledge,
                knowledge_results,
                retrieval_query,
                entity_terms=knowledge_entity_terms,
            )
            vector_results = materialize_durable(
                self._knowledge,
                vector_results,
                retrieval_query,
                entity_terms=knowledge_entity_terms,
            )

        promote_linked_documents = getattr(
            type(self._knowledge),
            "promote_documents_linked_to_knowledge",
            None,
        )
        if callable(promote_linked_documents):
            try:
                linked_document_results = promote_linked_documents(
                    self._knowledge,
                    [*knowledge_results, *vector_results],
                    retrieval_query,
                    top_k=effective_top_k,
                    entity_terms=knowledge_entity_terms,
                )
            except Exception as exc:
                logger.warning("关联知识反向提升文档失败，保留原召回结果: %s", exc)
                linked_document_results = []
            if linked_document_results:
                promoted_keys = {
                    chunk.doc_key for chunk in linked_document_results if chunk.doc_key
                }
                knowledge_results = [
                    *linked_document_results,
                    *(
                        chunk
                        for chunk in knowledge_results
                        if not chunk.doc_key or chunk.doc_key not in promoted_keys
                    ),
                ][:knowledge_top_k]
                logger.info("关联知识反向提升文档: %s 条", len(linked_document_results))

        keyword_weight = _KEYWORD_RRF_WEIGHT if vector_results else 1.0
        min_rrf_score = (
            _LOOKUP_MIN_RRF_SCORE_WITH_VECTOR
            if vector_results
            else 0.0
        )
        merged = reciprocal_rank_fusion(
            [
                (knowledge_results, keyword_weight),
                (vector_results, _VECTOR_RRF_WEIGHT),
                (pending_document_results, _PENDING_DOCUMENT_RRF_WEIGHT),
            ],
            top_k=max(effective_top_k * 2, 6),
            min_score=min_rrf_score,
        )
        if knowledge_results:
            merged = _append_missing_artifact_candidates(
                merged,
                knowledge_results,
                limit=max(effective_top_k * 3, 12),
            )
        selected_contexts = self._select_contexts(
            merged,
            effective_top_k,
            rescue_priority_terms=self._lexical_priority_terms(retrieval_query),
        )
        retrieval_finished = time.perf_counter()
        logger.info(
            "RAG 召回耗时 embedding_ms=%d knowledge_ms=%d pending_document_ms=%d "
            "vector_ms=%d merge_ms=%d total_ms=%d",
            round((embedding_finished - retrieval_started) * 1000),
            round(
                (
                    knowledge_finished
                    - embedding_finished
                    + knowledge_fallback_finished
                    - pending_document_finished
                )
                * 1000
            ),
            round((pending_document_finished - knowledge_finished) * 1000),
            round((vector_finished - knowledge_fallback_finished) * 1000),
            round((retrieval_finished - vector_finished) * 1000),
            round((retrieval_finished - retrieval_started) * 1000),
        )
        if on_contexts:
            on_contexts(selected_contexts)

        if references_only:
            return RagResult(
                answer="",
                contexts=selected_contexts,
                model="references-only",
            )

        context_text = self._build_context(selected_contexts)

        link_rule = (
            "用户正在询问地址/链接/网址。若上下文中包含 URL，请在回答中直接给出完整 URL，并用 Markdown 链接格式展示。\n"
            if _is_link_query(user_query) else ""
        )
        is_floating_assist_query = "## 用户问题理解" in user_query and "## 回答" in user_query
        floating_format_rule = ""
        if is_floating_assist_query:
            floating_format_rule = (
                "必须严格按以下 Markdown 结构输出，不能省略任何章节：\n"
                "## 用户问题理解\n"
                "用一句话说明你判断出的用户真实问题。\n"
                "## 回答\n"
                "直接给出结论和依据，不要反问，不要只给追问话术。\n\n"
            )
        time_clause = _build_relative_time_clause(intent)
        prompt = f"{link_rule}{floating_format_rule}{time_clause}工作记录上下文：\n{context_text}\n\n用户问题：{user_query}"

        # 注入用户身份说明（通用能力，不依赖任务类型）
        user_identity = self._read_user_identity()
        identity_clause = self._build_identity_clause(user_identity)
        system = self._system + identity_clause

        llm_kwargs = {}
        if is_floating_assist_query:
            llm_kwargs["num_predict"] = 8192
            llm_kwargs["temperature"] = 0.2
            llm_kwargs["top_p"] = 0.8

        primary_llm = llm or self._llm

        try:
            if on_delta:
                llm_resp = primary_llm.complete_stream(
                    prompt,
                    system=system,
                    on_delta=on_delta,
                    **llm_kwargs,
                )
            else:
                llm_resp = primary_llm.complete(prompt, system=system, **llm_kwargs)
            answer = llm_resp.text
        except Exception:
            raise

        answer = _attach_document_links(answer, selected_contexts, self._db_path)
        if _is_link_query(user_query):
            answer = _ensure_link_answer(answer, selected_contexts)

        return RagResult(
            answer=answer,
            contexts=selected_contexts,
            model=llm_resp.model,
            tokens=llm_resp.tokens,
            done_reason=llm_resp.done_reason,
            output_truncated=_is_output_truncated(llm_resp.done_reason),
        )

    @staticmethod
    def _build_context(chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return "（无相关工作记录）"
        parts = []
        for i, chunk in enumerate(chunks, 1):
            source = chunk.metadata.get("source_type") or chunk.source
            observed_at = chunk.metadata.get("observed_at")
            event_start = chunk.metadata.get("event_time_start")
            event_end = chunk.metadata.get("event_time_end")
            history_view = chunk.metadata.get("history_view")
            activity_type = chunk.metadata.get("activity_type")
            content_origin = chunk.metadata.get("content_origin")
            importance = chunk.metadata.get("importance")
            record_time = chunk.metadata.get("updated_at") or chunk.metadata.get("time")
            text = chunk.text[:_MAX_CHUNK_LEN]
            prefix: list[str] = [f"[{i}][{source}]"]
            if observed_at:
                prefix.append(f"看到时间={_format_ts(observed_at)}")
            if event_start or event_end:
                if event_start and event_end and event_start != event_end:
                    prefix.append(f"事件时间={_format_ts(event_start)}~{_format_ts(event_end)}")
                else:
                    prefix.append(f"事件时间={_format_ts(event_start or event_end)}")
            if history_view:
                prefix.append("历史回看")
            if activity_type:
                prefix.append(f"活动={activity_type}")
            if content_origin:
                prefix.append(f"来源={content_origin}")
            # 烘焙产物与待烘焙采集没有看到/事件时间，用创建或更新时间佐证周期归属
            if record_time and source in {"document", "bake_knowledge", "operation", "data", "pending_document"}:
                time_label = "采集时间" if source == "pending_document" else "创建/更新时间"
                prefix.append(f"{time_label}={_format_ts(record_time)}")
            # importance 仅作内部排序依据，不放入上下文文本（避免 LLM 在输出中暴露元数据）
            parts.append(f"{' '.join(prefix)} {text}")
        return "\n\n".join(parts)

    def _lexical_priority_terms(self, retrieval_query: str) -> list[tuple[str, float]]:
        """用与关键词通道一致的选词标准，提取补位排序优先词。

        补位候选里既有标题/正文直接命中查询词的产物，也有关联知识提权但
        与查询词无关的产物；后者的提权分更高，直接按分排序会把直接命中顶掉。
        每个词携带 idf 权重，罕见词（如产品名）的命中压过常见词（如“更新”）。
        """
        try:
            import sqlite3

            from rag.query_planner import build_artifact_query_plan

            with sqlite3.connect(self._db_path) as conn:
                plan = build_artifact_query_plan(conn.cursor(), retrieval_query)
            terms: list[tuple[str, float]] = []
            seen: set[str] = set()
            for term in [*plan.discriminative_terms, *plan.fallback_terms]:
                text = str(getattr(term, "text", term) or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                idf = float(getattr(term, "idf", 0.0) or 0.0)
                terms.append((text, idf))
            return terms
        except Exception as exc:
            logger.warning("提取补位优先词失败，回退按分排序: %s", exc)
            return []

    @staticmethod
    def _select_contexts(
        chunks: list[RetrievedChunk],
        top_k: int,
        rescue_priority_terms: Optional[list[tuple[str, float]]] = None,
    ) -> list[RetrievedChunk]:
        selected: list[RetrievedChunk] = []
        selected_keys: set[str] = set()
        logger.info("_select_contexts: 输入 %s 条，top_k=%s", len(chunks), top_k)

        def try_select(chunk: RetrievedChunk, selection_origin: str) -> bool:
            source_type = chunk.metadata.get("source_type") or chunk.source
            allowed_source_types = {
                "knowledge", "document", "pending_document", "bake_knowledge", "operation", "data"
            }
            if source_type not in allowed_source_types:
                logger.info(f"  跳过: source_type={source_type}")
                return False
            if _is_noise_chunk(chunk):
                logger.info(f"  跳过: 噪音chunk")
                return False

            identity_keys = _chunk_identity_keys(chunk)
            if not identity_keys or not identity_keys.isdisjoint(selected_keys):
                logger.info(f"  跳过: doc_key重复或为空")
                return False
            if source_type == "knowledge" and any(
                str(chunk.metadata.get("knowledge_id")) in _source_ids_of(selected_chunk)
                for selected_chunk in selected
            ):
                logger.info("  跳过: 时间线已有对应产物入选")
                return False
            logger.info(
                "  ✓ 选中: origin=%s importance=%s activity=%s",
                selection_origin,
                chunk.metadata.get("importance"),
                chunk.metadata.get("activity_type"),
            )
            selected.append(chunk)
            selected_keys.update(identity_keys)
            for linked_id in _source_ids_of(chunk):
                selected_keys.add(f"knowledge:{linked_id}")
            return True

        # 所有问题统一沿用 RRF 顺序；importance、证据强度等只作为融合/重排
        # 的连续信号，不能再通过离散模式整体替换排序规则。
        fused_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("selection_origin") != "artifact_rescue"
        ]
        rescue_chunks = [
            chunk
            for chunk in chunks
            if (chunk.metadata or {}).get("selection_origin") == "artifact_rescue"
        ]
        if rescue_priority_terms:
            # 直接命中查询词的产物优先于靠关联知识提权进来的候选；
            # 命中按 idf 加权，罕见词（如产品名）压过常见词（如“更新”），
            # 同权重内仍按原始分排序。
            def _priority(chunk: RetrievedChunk) -> float:
                haystack = str(
                    (chunk.metadata or {}).get("title") or ""
                ) + str(chunk.text or "")
                return sum(
                    weight for term, weight in rescue_priority_terms if term in haystack
                )

            rescue_chunks.sort(key=_priority, reverse=True)
        # 补位槽位按强词法候选数量弹性分配：同一查询可能直接命中多个产物
        # （如产品名同时命中多篇文档），固定 1 个槽会让次高命中被单个高分
        # 候选压制；top_k 越大，留给词法直接命中的保底空间越多。
        rescue_slots = (
            min(len(rescue_chunks), max(1, top_k // 4))
            if top_k >= 3 and rescue_chunks
            else 0
        )
        fused_target = max(0, top_k - rescue_slots)

        for chunk in fused_chunks:
            if len(selected) >= fused_target:
                break
            try_select(chunk, "rrf")

        rescue_taken = 0
        if rescue_slots:
            for chunk in rescue_chunks:
                if rescue_taken >= rescue_slots:
                    break
                if try_select(chunk, "artifact_rescue"):
                    rescue_taken += 1

        if not rescue_taken or len(selected) < top_k:
            for chunk in fused_chunks:
                if len(selected) >= top_k:
                    break
                try_select(chunk, "rrf")

        logger.info(f"_select_contexts: 最终选中 {len(selected)} 条")
        return selected

    @staticmethod
    def _parse_query_intent(user_query: str) -> QueryIntent:
        now_ms = int(time.time() * 1000)
        start_ts: Optional[int] = None
        end_ts: Optional[int] = now_ms
        observed_start_ts: Optional[int] = None
        observed_end_ts: Optional[int] = None
        event_start_ts: Optional[int] = None
        event_end_ts: Optional[int] = None
        target_time_semantics = "either"
        period_kind = ""
        period_phrase = ""

        # ── 时间范围解析 ─────────────────────────────────────────────────
        if "上周" in user_query:
            # 上周：上周一 00:00 ~ 本周一 00:00 - 1ms
            this_week_start = _week_start_ms()
            start_ts = this_week_start - 7 * 24 * 60 * 60 * 1000
            end_ts = this_week_start - 1
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            period_kind, period_phrase = "previous_week", "上周"
        elif "最近" in user_query:
            start_ts = now_ms - 7 * 24 * 60 * 60 * 1000
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            period_kind, period_phrase = "recent", "最近"
        elif "今天" in user_query:
            start_ts = _day_start_ms(0)
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            period_kind, period_phrase = "today", "今天"
        elif "昨天" in user_query:
            start_ts = _day_start_ms(-1)
            end_ts = _day_start_ms(0) - 1
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            event_start_ts = start_ts
            event_end_ts = end_ts
            period_kind, period_phrase = "yesterday", "昨天"
        elif "本周" in user_query:
            start_ts = _week_start_ms()
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            period_kind, period_phrase = "current_week", "本周"

        # 生成确定性的周期描述（含周次与起止日期），供 prompt 时间口径使用
        period_display = ""
        if period_kind and start_ts is not None:
            if period_kind == "current_week":
                display_end = start_ts + 7 * 24 * 60 * 60 * 1000 - 1
            elif period_kind == "recent":
                display_end = now_ms
            else:
                display_end = end_ts if end_ts is not None else now_ms
            start_text = _format_date_cn(start_ts)
            end_text = _format_date_cn(display_end)
            period_display = start_text if start_text == end_text else f"{start_text} 至 {end_text}"
            if period_kind in ("current_week", "previous_week"):
                iso_year, iso_week = _iso_week_of(start_ts)
                if iso_year and iso_week:
                    period_display = f"{iso_year} 年第 {iso_week} 周（{period_display}）"

        entity_terms = _extract_query_terms(user_query)
        app_names = [term for term in entity_terms if any(ch.isascii() for ch in term)]

        source_types: list[str] = []
        if any(token in user_query for token in ("知识", "总结", "结论", "概述")):
            source_types.append("knowledge")
        if any(token in user_query for token in ("原文", "记录", "截图", "窗口", "应用")):
            source_types.append("capture")

        category = None
        if "会议" in user_query:
            category = "会议"
        elif "文档" in user_query:
            category = "文档"
        elif "代码" in user_query:
            category = "代码"
        elif "聊天" in user_query:
            category = "聊天"

        activity_types: list[str] = []
        content_origins: list[str] = []
        history_view: Optional[bool] = None
        is_self_generated: Optional[bool] = False
        evidence_strengths: list[str] = []
        # 明确的相对时间天然按“看到时间”过滤，不再先把问题手工划分成
        # lookup/summary。问题是查地址还是写总结，只影响答案生成，不切换召回算法。
        if period_kind:
            target_time_semantics = "observed"

        if target_time_semantics == "event" and observed_start_ts is not None:
            observed_start_ts = None
            observed_end_ts = None
        elif target_time_semantics == "observed" and start_ts is not None:
            event_start_ts = None
            event_end_ts = None

        return QueryIntent(
            start_ts=start_ts,
            end_ts=end_ts,
            observed_start_ts=observed_start_ts,
            observed_end_ts=observed_end_ts,
            event_start_ts=event_start_ts,
            event_end_ts=event_end_ts,
            entity_terms=entity_terms,
            app_names=app_names,
            source_types=source_types,
            category=category,
            target_time_semantics=target_time_semantics,
            activity_types=activity_types,
            content_origins=content_origins,
            history_view=history_view,
            is_self_generated=is_self_generated,
            evidence_strengths=evidence_strengths,
            period_kind=period_kind,
            period_phrase=period_phrase,
            period_display=period_display,
        )


def _extract_query_terms(query: str) -> list[str]:
    import re

    tokens = re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]+", query.lower())
    terms: list[str] = []
    seen: set[str] = set()
    stop_terms = {
        "什么", "怎么", "如何", "为什么", "昨天", "今天", "最近", "本周", "那段",
        "提到", "知识", "总结", "里", "了吗", "是否", "有关", "关于", "做了什么",
        "干了什么", "做过什么", "问了什么", "看了什么", "历史消息", "历史记录", "历史对话",
        "工作", "工作有", "有哪些", "哪些", "进展", "内容",
    }

    def _add(term: str) -> None:
        term = term.strip()
        if len(term) < 2 or term in stop_terms or term in seen:
            return
        seen.add(term)
        terms.append(term)

    for token in tokens:
        if len(token) < 2:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if len(token) <= 4:
                _add(token)
                continue
            meaningful_subterms: list[str] = []
            for size in (4, 3, 2):
                for i in range(0, len(token) - size + 1):
                    candidate = token[i:i + size]
                    if candidate in stop_terms:
                        continue
                    if any(mark in candidate for mark in ("工作", "总结", "哪些", "最近", "今天", "昨天", "本周")):
                        continue
                    meaningful_subterms.append(candidate)
            if meaningful_subterms:
                for candidate in meaningful_subterms:
                    _add(candidate)
            else:
                _add(token)
        else:
            _add(token)

    return terms


def _looks_like_noise_chunk(chunk: RetrievedChunk) -> bool:
    metadata = chunk.metadata or {}
    text = (chunk.text or "").strip()
    activity_type = metadata.get("activity_type")
    content_origin = metadata.get("content_origin")
    evidence_strength = metadata.get("evidence_strength")
    return text.startswith("概述：低价值工作片段（") or text.startswith("低价值工作片段（") or (
        evidence_strength == "low"
        and activity_type in {None, "other"}
        and content_origin in {None, "other"}
    )


def _is_noise_chunk(chunk: RetrievedChunk) -> bool:
    metadata = chunk.metadata or {}
    overview = str(metadata.get("overview") or "")
    if overview.startswith("低价值工作片段（"):
        return True
    return _looks_like_noise_chunk(chunk)


def _source_ids_of(chunk: RetrievedChunk) -> set[str]:
    metadata = chunk.metadata or {}
    ids: set[str] = set()
    for key in ("source_timeline_ids", "linked_knowledge_ids"):
        value = metadata.get(key)
        if isinstance(value, list):
            ids.update(str(item) for item in value if item is not None)
        elif isinstance(value, str) and value.strip():
            ids.update(re.findall(r"\d+", value))
    return ids


def _chunk_identity_keys(chunk: RetrievedChunk) -> set[str]:
    """返回可以指向同一参考资料的所有稳定键。

    文档会分别从向量、产物关键词和 URL 兜底通道进入召回，同一份
    文档因此可能携带 ``document:<id>`` 或 ``document_url:<url>``。同时
    保留 ID 和规范化 URL 别名，使最终候选合并不依赖某一条通道的键格式。
    """
    metadata = chunk.metadata or {}
    keys: set[str] = set()
    doc_key = chunk.doc_key or metadata.get("doc_key")
    if doc_key:
        keys.add(str(doc_key))

    source_type = str(metadata.get("source_type") or chunk.source or "")
    if source_type not in {"document", "pending_document"}:
        return keys

    document_id = metadata.get("document_id") or metadata.get("artifact_id")
    if document_id is not None and str(document_id).strip():
        keys.add(f"document:{document_id}")

    url_key = _document_url_doc_key(metadata.get("source_url") or metadata.get("url"))
    if url_key:
        keys.add(url_key)
    return keys


def _append_missing_artifact_candidates(
    merged: list[RetrievedChunk],
    candidates: list[RetrievedChunk],
    limit: int,
) -> list[RetrievedChunk]:
    """Keep high-confidence bake artifacts visible after RRF truncation.

    Keyword artifact search already ranks documents/knowledge/SOPs by title and body matches.
    When vector results dominate the RRF top slice, a directly matched document can be dropped
    before _select_contexts has a chance to reserve a bounded rescue slot.
    """
    if len(merged) >= limit:
        return merged

    seen: set[str] = set()
    for chunk in merged:
        seen.update(_chunk_identity_keys(chunk))
    appended: list[RetrievedChunk] = []
    for chunk in candidates:
        source_type = (chunk.metadata or {}).get("source_type") or chunk.source
        # Legacy timeline knowledge commonly carries a normalized score below 1.0 and is
        # still a primary, directly matched source. Bake artifacts use FTS-style scores;
        # only append those when the keyword signal is strong so weak document matches do
        # not leak back after the vector-aware RRF threshold filtered them out.
        is_primary_knowledge = source_type == "knowledge"
        is_strong_bake_artifact = (
            source_type in {"document", "bake_knowledge", "operation", "data"}
            and float(chunk.score or 0) >= 5.0
        )
        if not is_primary_knowledge and not is_strong_bake_artifact:
            continue
        doc_key = chunk.doc_key or (chunk.metadata or {}).get("doc_key")
        identity_keys = _chunk_identity_keys(chunk)
        if not doc_key or not identity_keys.isdisjoint(seen):
            continue
        appended.append(
            RetrievedChunk(
                capture_id=chunk.capture_id,
                text=chunk.text,
                score=chunk.score,
                source=chunk.source,
                doc_key=doc_key,
                metadata={
                    **(chunk.metadata or {}),
                    "doc_key": doc_key,
                    "selection_origin": "artifact_rescue",
                },
            )
        )
        seen.update(identity_keys)
        if len(merged) + len(appended) >= limit:
            break

    return [*merged, *appended]


def _build_pending_document_candidates(
    chunks: list[RetrievedChunk],
    query: str,
    limit: int,
) -> list[RetrievedChunk]:
    """Promote raw full-text hits only when they are substantive document captures.

    Capture embeddings intentionally contain only a short AX prefix today, while
    SQLite FTS indexes the full AX body. A match-centred excerpt keeps a keyword
    after the first 500/800 characters visible to the answer model.
    """
    results: list[RetrievedChunk] = []
    seen_urls: set[str] = set()
    terms = _extract_query_terms(query)

    for chunk in chunks:
        metadata = chunk.metadata or {}
        url = str(metadata.get("url") or metadata.get("source_url") or "").strip()
        canonical_url = _normalize_document_url(url)
        text = str(chunk.text or "").strip()
        if not canonical_url or not _looks_like_document_url(canonical_url) or len(text) < 200:
            continue
        if canonical_url in seen_urls:
            continue

        excerpt = _match_centered_excerpt(text, terms, max_chars=620)
        title = str(metadata.get("webpage_title") or metadata.get("win_title") or "待烘焙文档").strip()
        doc_key = _document_url_doc_key(canonical_url)
        results.append(
            RetrievedChunk(
                capture_id=chunk.capture_id,
                text=f"页面：{title}\nURL：{canonical_url}\n正文片段：{excerpt}",
                score=max(float(chunk.score or 0), 5.0),
                source="capture_fts",
                doc_key=doc_key,
                metadata={
                    **metadata,
                    "doc_key": doc_key,
                    "source_type": "pending_document",
                    "source_url": canonical_url,
                    "url": canonical_url,
                    "title": title,
                    "category": "文档",
                    "activity_type": "reading",
                    "content_origin": "document_reference",
                    "evidence_strength": "medium",
                    "retrieval_method": "capture_fulltext",
                },
            )
        )
        seen_urls.add(canonical_url)
        if len(results) >= limit:
            break

    return results


def _match_centered_excerpt(text: str, terms: list[str], max_chars: int) -> str:
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in terms if term and lowered.find(term.lower()) >= 0]
    if not positions:
        return text[:max_chars]
    match_pos = min(positions)
    start = max(0, match_pos - min(160, max_chars // 3))
    return text[start:start + max_chars]


def _normalize_document_url(url: str) -> str:
    return re.split(r"[?#]", str(url or "").strip(), maxsplit=1)[0].rstrip("/")


def _looks_like_document_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(
        marker in lowered
        for marker in (
            "/docs/",
            "docs.google.",
            "/document/",
            "yuque.",
            "feishu.",
            "notion.",
            "confluence",
            "/wiki/",
            "shimo.",
            "/d/home/",
            "/s/home/",
            "/k/home/",
        )
    )


_DOC_LINK_LINE_RE = re.compile(r"URL[:：]\s*(https?://[^\s)）】]+)")
_DOC_TITLE_LINE_RE = re.compile(r"(?:文档|页面)[:：]\s*([^\n]+)")
_MENTION_RE = re.compile(r"《([^《》\n]{2,80})》")


def _normalize_doc_title(title: str) -> str:
    """归一化文档标题，用于答案提及与候选链接的模糊对齐。

    去掉空白与书名号，并剥离云文档平台统一追加的「- 云文档」后缀，
    使《稳柱 - 收入巡检归因复盘》能匹配烘焙标题「稳柱-收入巡检归因复盘 - 云文档」。
    """
    text = re.sub(r"\s+", "", str(title or ""))
    text = text.strip("《》「」")
    for suffix in ("(云文档)", "（云文档）"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    for suffix in ("-云文档", "云文档"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip("《》「」")


def _collect_document_links(chunks: list[RetrievedChunk]) -> list[tuple[str, str]]:
    """从参考资料中收集 (标题, URL) 对，按出现顺序去重。"""
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in chunks:
        metadata = chunk.metadata or {}
        url = str(metadata.get("source_url") or metadata.get("url") or "").strip()
        text = chunk.text or ""
        if not url:
            match = _DOC_LINK_LINE_RE.search(text)
            url = match.group(1) if match else ""
        if not url or url in seen:
            continue
        title = str(metadata.get("title") or "").strip()
        if not title:
            match = _DOC_TITLE_LINE_RE.search(text)
            title = match.group(1).strip() if match else ""
        if not title:
            continue
        seen.add(url)
        links.append((title, url))
    return links


def _baked_document_link_index(db_path: Optional[str]) -> dict:
    """烘焙文档「归一化标题 -> (标题, URL)」索引，为无 URL 的召回记录补链接。"""
    index: dict = {}
    if not db_path:
        return index
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bake_documents'"
        )
        if not cursor.fetchone():
            conn.close()
            return index
        cursor.execute(
            "SELECT title, source_url FROM bake_documents "
            "WHERE deleted_at IS NULL AND source_url IS NOT NULL AND source_url != ''"
        )
        for title, url in cursor.fetchall():
            key = _normalize_doc_title(title)
            if key and key not in index:
                index[key] = (str(title).strip(), str(url).strip())
        conn.close()
    except Exception as exc:
        logger.warning("查询烘焙文档链接失败，跳过链接兜底: %s", exc)
    return index


def _lookup_baked_mention(index: dict, norm_mention: str) -> Optional[tuple]:
    """按答案提及查烘焙文档：精确匹配优先；「日期+标题」后缀场景仅唯一命中才采纳，
    避免多篇同名日报时误链到错误日期版本。"""
    if not norm_mention:
        return None
    exact = index.get(norm_mention)
    if exact:
        return exact
    matches = [value for key, value in index.items() if key.endswith(norm_mention)]
    return matches[0] if len(matches) == 1 else None


def _attach_document_links(
    answer: str,
    chunks: list[RetrievedChunk],
    db_path: Optional[str] = None,
) -> str:
    """为答案中提及的文档补上超链接。

    模型常只提文档名不给链接；这里依据参考资料自带 URL 与烘焙文档标题索引，
    对答案中确实提到的文档补充 Markdown 超链接：优先原位替换《文档名》式提及，
    无法原位替换的汇总到文末「相关文档链接」列表。未提及的文档不追加链接。
    """
    text = str(answer or "")
    if not text.strip():
        return answer

    norm_answer = _normalize_doc_title(text)
    mentions = [
        (match.start(), match.end(), match.group(1))
        for match in _MENTION_RE.finditer(text)
    ]

    replacements: list[tuple[int, int, str]] = []
    section: list[tuple[str, str]] = []
    used_urls: set[str] = set()

    def try_claim(title: str, url: str) -> None:
        if not url or url in text or url in used_urls:
            return
        norm = _normalize_doc_title(title)
        if not norm or len(norm) < 4 or norm not in norm_answer:
            return
        used_urls.add(url)
        for start, end, inner in mentions:
            if _normalize_doc_title(inner) != norm:
                continue
            if text[end:end + 2] == "](":
                return  # 该提及已是超链接，无需重复补充
            if any(not (end <= taken_start or start >= taken_end) for taken_start, taken_end, _ in replacements):
                section.append((title, url))
                return
            replacements.append((start, end, f"[《{inner}》]({url})"))
            return
        section.append((title, url))

    for title, url in _collect_document_links(chunks):
        try_claim(title, url)
    baked_index = _baked_document_link_index(db_path)
    if baked_index:
        for _start, _end, inner in mentions:
            baked = _lookup_baked_mention(baked_index, _normalize_doc_title(inner))
            if baked:
                try_claim(inner, baked[1])
        # 答案提及可能缺少书名号（如模型输出的标题残缺），再按标题全量兜底到文末列表。
        for title, url in baked_index.values():
            try_claim(title, url)

    if not replacements and not section:
        return answer
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    if section:
        lines = [f"- [{title}]({url})" for title, url in section]
        text = text.rstrip() + "\n\n相关文档链接：\n" + "\n".join(lines)
    return text


def _is_link_query(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in ("url", "链接", "地址", "网址", "文档地址", "页面"))


def _ensure_link_answer(answer: str, contexts: list[RetrievedChunk]) -> str:
    link_items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in contexts:
        metadata = chunk.metadata or {}
        url = str(metadata.get("source_url") or metadata.get("url") or "").strip()
        if not url:
            match = re.search(r"https?://[^\s)）】]+", chunk.text or "")
            url = match.group(0) if match else ""
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(metadata.get("title") or metadata.get("overview") or metadata.get("summary") or "相关文档").strip()
        link_items.append((title, url))
        if len(link_items) >= 3:
            break

    if not link_items:
        return answer

    if any(url in answer for _, url in link_items):
        return answer

    lines = ["找到的文档地址："]
    for title, url in link_items:
        lines.append(f"- [{title}]({url})")
    return "\n".join(lines)


def _day_start_ms(offset_days: int) -> int:
    now = time.localtime()
    midnight = time.mktime((
        now.tm_year,
        now.tm_mon,
        now.tm_mday,
        0,
        0,
        0,
        now.tm_wday,
        now.tm_yday,
        now.tm_isdst,
    ))
    return int((midnight + offset_days * 24 * 60 * 60) * 1000)



def _week_start_ms() -> int:
    now = time.localtime()
    start_today_ms = _day_start_ms(0)
    return start_today_ms - now.tm_wday * 24 * 60 * 60 * 1000


def _format_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))
    except Exception:
        return str(ts)
