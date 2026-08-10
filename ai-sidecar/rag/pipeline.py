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
    "如果上下文中没有相关信息，请直接说明。"
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

_WEEKLY_REPORT_SYSTEM_PROMPT = (
    "你是记忆面包，一个本地运行的 AI 工作助手。"
    "你的任务是：直接根据下方【工作记录上下文】生成一份工作周报，立即输出，不得提问、不得要求用户补充任何信息。\n"
    "【强制规则】\n"
    "- 禁止询问用户任何问题\n"
    "- 禁止输出'请提供'、'请告诉我'等请求性语句\n"
    "- 没有足够数据的章节直接跳过，不输出该章节标题，不输出'无相关内容'等占位文字\n"
    "【写作风格】\n"
    "- 省略所有主语（人名/他/她），直接以动词开头描述做了什么\n"
    "- 语言简洁专业，每条内容不少于2句话，说明做了什么、为何重要、达到什么效果\n"
    "【标题规则】每条工作项使用「- **短标题**：详细描述」格式，其中：\n"
    "- 短标题必须是5个字以内的动宾短语，概括该条工作的核心动作和对象，例如：「Logo设计」「指标评审」「监控排查」\n"
    "- 绝不能把描述性长句作为标题，绝不能写占位文字\n"
    "【内容取舍】\n"
    "- 只使用工作记录中 activity_type 标注为 coding、meeting、chat 的内容\n"
    "- reading 类（查看/阅读文档）一律不写入周报\n"
    "- 工具报错、系统告警、应用切换等纯系统操作一律省略\n"
    "【输出规范】禁止在输出中出现任何元数据标记（如 importance=、活动=、来源= 等），输出直接可用的正式周报\n"
    "【证据规范】当上下文存在“量化证据”区块时，所有数字化进展必须引用证据编号；无证据禁止编造数字。"
)

_PROJECT_WEEKLY_REPORT_SYSTEM_PROMPT = (
    "你是记忆面包，一个本地运行的 AI 工作助手。"
    "你的任务是：直接根据下方【项目工作记录上下文】生成项目周报，立即输出，不得提问、不得要求用户补充任何信息。\n"
    "【强制规则】\n"
    "- 禁止询问用户任何问题\n"
    "- 禁止输出'请提供'、'请告诉我'等请求性语句\n"
    "- 只写可验证结论，不写活动流水\n"
    "【结构规范】固定输出：本周核心产出、项目进展、本周量化进展（OKR/KPI/专项，如有）、下周计划、风险/阻塞\n"
    "【证据规范】数字化结论必须可追溯到参考资料编号（如 R#1）；无证据不得输出数字。\n"
    "【输出规范】禁止输出表格、元数据标记、时间戳、应用名、窗口名、来源字段。"
)

_DAILY_REPORT_SYSTEM_PROMPT = (
    "你是记忆面包，一个本地运行的 AI 工作助手。"
    "根据以下今天的工作记录，帮用户生成一份详尽完整的工作日报。\n"
    "【写作风格】\n"
    "- 以第一人称工作视角描述，省略所有主语（人名/用户/他/她），直接描述做了什么\n"
    "- 错误示例：'鲜嘉麒参与了XX会议'、'他查看了XX文档'→ 正确示例：'参与了XX会议'、'查看了XX文档'\n"
    "【篇幅要求】\n"
    "- 每条工作内容需展开描述，说明做了什么、为什么做、达到了什么效果，不少于2句话\n"
    "- 有记录的章节至少输出3条\n"
    "- 今日小结不少于3句话，覆盖工作量、进展、遇到的问题或明日计划\n"
    "【内容取舍】每条记录带有重要性评分（importance 1-5）：\n"
    "- importance >= 3：正常展示并展开描述\n"
    "- importance <= 2：省略或归并为'其他零散操作'\n"
    "- 工具报错、系统告警、应用切换等纯系统操作一律省略\n"
    "【输出规范】输出内容中绝对禁止出现任何元数据标记（如 (重要性:3)、importance=4 等），输出直接可用的正式日报内容。\n"
    "【格式要求 - 严格遵守】\n"
    "1. 必须使用 Markdown 格式，按活动类型分组，每个分组必须以 ## 开头（如：## 开发、## 会议、## 沟通、## 阅读、## 其他）\n"
    "2. 每个 ## 分组下，必须使用一级列表（- 开头），每条列表项后换行展开描述，禁止直接写段落文本\n"
    "3. 格式示例：\n"
    "   ## 开发\n"
    "   - 完成了XX功能的实现\n"
    "     详细描述做了什么、为什么做、达到了什么效果。\n"
    "   - 修复了YY问题\n"
    "     说明问题原因和解决方案。\n"
    "4. 末尾必须有 ## 今日小结 章节，用段落格式（不用列表），不少于3句话\n"
    "5. 如果某类工作没有记录，必须完全省略该分组，不要生成该章节标题，绝对禁止输出'无相关内容'等占位文本\n"
    "6. 只基于提供的记录生成，不要编造内容\n"
    "7. 禁止使用表格、禁止使用多层缩进的嵌套列表"
)

_PROJECT_SUMMARY_SYSTEM_PROMPT = (
    "你是记忆面包，一个本地运行的 AI 工作助手。"
    "根据以下项目相关的工作记录，帮用户生成一份结构清晰、内容详尽的项目总结报告。"
    "【篇幅要求】\n"
    "- 每个章节至少写3条，每条展开描述不少于2句话\n"
    "- 总篇幅应充分反映项目的实际工作投入，不因'简洁'省略有价值内容\n"
    "要求：\n"
    "1. 用 Markdown 格式输出，包含以下章节：项目背景与目标、主要完成内容、关键决策与方案、"
    "遇到的挑战及解决方案、成果与数据、经验教训与改进建议\n"
    "2. 每个章节均需详细展开，用具体的技术细节和数据支撑\n"
    "3. 如果某章节没有足够记录，可简要说明\n"
    "4. 最后加「下一步计划」章节（如有迹象可循），至少3条\n"
    "5. 只基于提供的记录生成，不要编造内容"
)

_MAX_CHUNK_LEN = 800   # 单个上下文片段最大字符数
_KEYWORD_RRF_WEIGHT = 0.45
_PENDING_DOCUMENT_RRF_WEIGHT = 0.7
_VECTOR_RRF_WEIGHT = 1.0
# 关键词知识首位在 0.45 权重、RRF k=60 时的分数约为 0.00738。
# 阈值高于该值会在向量召回存在时误删直接命中的持久知识，只留下向量结果。
_LOOKUP_MIN_RRF_SCORE_WITH_VECTOR = 0.01
_VECTOR_LOOKUP_SCORE_THRESHOLD = 0.45
_VECTOR_SUMMARY_SCORE_THRESHOLD = 0.35


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
    query_mode: str = "lookup"
    activity_types: list[str] = field(default_factory=list)
    content_origins: list[str] = field(default_factory=list)
    history_view: Optional[bool] = None
    is_self_generated: Optional[bool] = None
    evidence_strengths: list[str] = field(default_factory=list)
    # 任务型意图：weekly_report | daily_report | project_summary | project_weekly_report | None
    task_type: Optional[str] = None
    # 是否启用 OKR/KPI/专项量化模式
    kpi_mode: bool = False


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
        effective_top_k = top_k or self._top_k
        intent = self._parse_query_intent(user_query)
        retrieval_query = _extract_core_retrieval_query(user_query)
        retrieval_started = time.perf_counter()

        # 报告类任务优先保证稳定返回，限制上下文规模，避免 prompt 过大导致本地模型超时
        if intent.task_type == "weekly_report":
            effective_top_k = max(effective_top_k, 18)
        elif intent.task_type == "daily_report":
            effective_top_k = max(effective_top_k, 12)
        elif intent.task_type == "project_summary":
            effective_top_k = max(effective_top_k, 24)
        elif intent.task_type == "project_weekly_report":
            effective_top_k = max(effective_top_k, 20)
        # 普通 summary 模式（如"总结我本周的工作"）：适度扩大
        elif intent.query_mode == "summary":
            effective_top_k = max(effective_top_k, 20)

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

        # 任务型意图：不按关键词过滤，纯按时间段和活动类型宽松召回
        knowledge_entity_terms = None if (intent.task_type or retrieval_query != user_query) else (intent.entity_terms or None)

        _is_report_task = intent.task_type in ("weekly_report", "daily_report", "project_weekly_report")
        logger.info(f"任务类型: {intent.task_type}, 是否报告任务: {_is_report_task}, start_ts: {intent.start_ts}, end_ts: {intent.end_ts}")
        knowledge_top_k = effective_top_k * 2
        if intent.query_mode == "lookup" and not intent.task_type:
            knowledge_top_k = max(effective_top_k * 6, 24)

        knowledge_results = self._knowledge.search(
            retrieval_query if not intent.task_type else "",
            top_k=knowledge_top_k,
            # 周报/日报：start_ts/end_ts 过滤 k.start_time/k.end_time（事件时间），与 created_at 时间段无关，置 None
            start_ts=None if _is_report_task else intent.start_ts,
            end_ts=None if _is_report_task else intent.end_ts,
            entity_terms=knowledge_entity_terms,
            # 周报/日报：用 created_at 过滤（知识生成时间），不用 observed_at（原始截图时间，可能是历史数据）
            observed_start_ts=None if _is_report_task else intent.observed_start_ts,
            observed_end_ts=None if _is_report_task else intent.observed_end_ts,
            event_start_ts=intent.event_start_ts,
            event_end_ts=intent.event_end_ts,
            activity_types=intent.activity_types or None,
            content_origins=intent.content_origins or None,
            history_view=intent.history_view,
            is_self_generated=intent.is_self_generated,
            evidence_strengths=intent.evidence_strengths or None,
            query_mode=intent.query_mode,
            created_start_ts=intent.start_ts if _is_report_task else None,
            created_end_ts=intent.end_ts if _is_report_task else None,
        ) if self._knowledge else []
        knowledge_finished = time.perf_counter()

        logger.info(f"知识库检索结果: {len(knowledge_results)} 条")

        # 尚未烘焙完成的文档也应能通过原始 capture 全文命中。这里只在普通
        # lookup 中启用，并且仅接纳带文档 URL 的结果，避免把普通屏幕噪声
        # 重新引入 RAG 上下文。
        pending_document_results: list[RetrievedChunk] = []
        if intent.query_mode == "lookup" and not intent.task_type:
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
            logger.info("待烘焙文档检索结果: %s 条", len(pending_document_results))
        pending_document_finished = time.perf_counter()

        # 周报/日报时间兜底：若本周/今天无数据，自动扩大到最近14天
        if intent.task_type in ("weekly_report", "project_weekly_report", "daily_report") and not knowledge_results:
            logger.info("周报任务无 knowledge 数据，回退到最近 14 天")
            fallback_start = int(time.time() * 1000) - 14 * 24 * 60 * 60 * 1000
            knowledge_results = self._knowledge.search(
                "",
                top_k=effective_top_k * 2,
                observed_start_ts=fallback_start,
                observed_end_ts=intent.observed_end_ts,
                activity_types=intent.activity_types or None,
                history_view=intent.history_view,
                is_self_generated=intent.is_self_generated,
                evidence_strengths=intent.evidence_strengths or None,
                query_mode=intent.query_mode,
                created_start_ts=fallback_start,
            ) if self._knowledge else []
            # 进一步兜底：若 activity_types 过滤后仍无数据，去掉 activity_types 限制再查
            if not knowledge_results:
                logger.info("带 activity_types 仍无数据，去掉过滤重试")
                knowledge_results = self._knowledge.search(
                    "",
                    top_k=effective_top_k * 2,
                    observed_start_ts=fallback_start,
                    observed_end_ts=intent.observed_end_ts,
                    history_view=intent.history_view,
                    is_self_generated=intent.is_self_generated,
                    query_mode=intent.query_mode,
                    created_start_ts=fallback_start,
                ) if self._knowledge else []
        knowledge_fallback_finished = time.perf_counter()
        vector_results = (
            self._vector.search(
                query_vector,
                top_k=effective_top_k * 3,
                score_threshold=(
                    _VECTOR_SUMMARY_SCORE_THRESHOLD
                    if intent.query_mode == "summary" or intent.task_type
                    else _VECTOR_LOOKUP_SCORE_THRESHOLD
                ),
                filters=VectorSearchFilter(
                    start_ts=intent.start_ts,
                    end_ts=intent.end_ts,
                    observed_start_ts=intent.observed_start_ts,
                    observed_end_ts=intent.observed_end_ts,
                    event_start_ts=intent.event_start_ts,
                    event_end_ts=intent.event_end_ts,
                    source_types=["knowledge", "document"],
                    app_names=None if (retrieval_query != user_query or intent.query_mode == "summary" or intent.task_type) else (intent.app_names or intent.entity_terms or None),
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
            if vector_results and intent.query_mode == "lookup" and not intent.task_type
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
        if intent.query_mode == "lookup" and knowledge_results:
            merged = _append_missing_artifact_candidates(
                merged,
                knowledge_results,
                limit=max(effective_top_k * 3, 12),
            )
        selected_contexts = self._select_contexts(merged, effective_top_k, query_mode=intent.query_mode)
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

        is_report = intent.task_type in ("weekly_report", "daily_report", "project_summary", "project_weekly_report")
        # 报告模式：提前读取身份，用于上下文主语去除
        user_identity = self._read_user_identity()
        user_names = [n.strip() for n in user_identity.split(",") if n.strip()] if user_identity else []
        context_text = self._build_context(selected_contexts, strip_user_subject=is_report, user_names=user_names if is_report else None)

        # 报告模式：对少量高价值知识补充 details，增强推理质量，同时控制 prompt 体积
        if is_report and selected_contexts and self._db_path:
            enriched_details = self._fetch_top_n_details(selected_contexts, top_n=4)
            if enriched_details:
                # 对详情文本同样做人名/代词去除
                if user_names:
                    enriched_details = _strip_user_subject(enriched_details, user_names=user_names)
                context_text += "\n\n【核心知识详情（请重点参考）】\n" + enriched_details

        quant_evidence_block = ""
        if intent.task_type in ("weekly_report", "project_weekly_report") and selected_contexts:
            quant_evidence_block = self._build_quant_evidence_block(
                selected_contexts,
                kpi_mode=intent.kpi_mode,
                top_n=10 if intent.kpi_mode else 6,
            )

        # 任务型意图：若无任何上下文，直接返回提示，不走 LLM（避免 LLM 自由发挥）
        if intent.task_type and not selected_contexts:
            type_name = {
                "weekly_report": "本周",
                "daily_report": "今天",
                "project_summary": "项目",
                "project_weekly_report": "本周项目",
            }.get(intent.task_type, "")
            return RagResult(
                answer=f"暂未找到{type_name}的工作记录，无法生成报告。请确认记忆面包已正常捕获屏幕内容。",
                contexts=[],
                model="no-context",
            )

        quant_evidence_section = f"\n\n{quant_evidence_block}\n" if quant_evidence_block else ""

        # 任务型意图：prompt 中明确标注「以下是真实工作记录」，强制 LLM 基于数据输出
        if intent.task_type == "weekly_report":
            kpi_section_rule = (
                "11. 若涉及 OKR/KPI/专项进展，必须新增“## 本周量化进展（OKR/KPI/专项）”章节；"
                "每条使用「- 指标结论（证据：R#序号）」格式。\n"
                "12. 量化结论必须来自“量化证据”区块；无证据不得输出任何数字。\n"
                if intent.kpi_mode else
                "11. 若上下文提供“量化证据”区块，优先引用其数字；无证据不得编造数字。\n"
            )
            prompt = (
                "【输出规则】\n"
                "1. 严格按以下 Markdown 结构输出；有数据的章节必须输出章节标题，没有数据的章节直接跳过（含标题），不写'无相关内容'等占位文字。\n"
                "2. 固定章节顺序：## 本周核心产出 → ## 项目进展"
                + (" → ## 本周量化进展（OKR/KPI/专项）" if intent.kpi_mode else "")
                + " → ## 下周计划 → ## 风险/阻塞。\n"
                "3. 【本周核心产出】下每条使用「- **短标题**：详细描述」格式；短标题是5个字以内的动宾短语，禁止输出“短标题”“详细描述”等占位词。\n"
                "4. 【本周核心产出】每条描述至少2句，只写结果、价值、影响；不要输出“（结果）”“（价值）”这类括号标签。\n"
                "5. 【项目进展】下每条使用「- **项目名**：已完成 / 进行中 / 待启动 — 进展说明」格式，禁止输出“项目名”占位词，禁止只输出状态集合或散列短句。\n"
                "6. 【下周计划】下每条使用「- 具体可交付目标」格式，必须可验收，不写“继续推进”“持续跟进”“调研”等空泛表述，也不要输出“具体可交付目标”占位词。\n"
                "7. 【风险/阻塞】下每条使用「- 风险点：影响范围 / 受阻原因」格式；没有真实风险则整节跳过，不要输出“风险点”占位词。\n"
                "8. 只使用工作记录中 activity_type=coding、meeting、chat 的内容；reading 类内容不得写入。\n"
                "9. 禁止输出表格、元数据标记、时间戳、应用名、窗口名、来源字段。\n"
                "10. 输出完周报正文后立即结束，禁止附带原始工作记录、禁止重复粘贴上下文。\n"
                + kpi_section_rule + "\n"
                f"以下是本周真实工作记录（共 {len(selected_contexts)} 条）：\n\n{context_text}"
                f"{quant_evidence_section}\n"
                "---\n"
                f"用户指令：{user_query}\n"
            )
        elif intent.task_type == "project_weekly_report":
            kpi_section_rule = (
                "6. 必须输出“## 本周量化进展（OKR/KPI/专项）”章节；每条量化结论都必须带参考资料编号（证据：R#序号）。\n"
                "7. 未在证据区块出现的数字禁止输出，无法验证时改为定性描述。\n"
                if intent.kpi_mode else
                "6. 若上下文提供量化证据，优先输出可验证数字并附参考资料编号（证据：R#序号）；无证据不要编造数字。\n"
            )
            prompt = (
                "【输出规则】\n"
                "1. 固定章节顺序：## 本周核心产出 → ## 项目进展"
                + (" → ## 本周量化进展（OKR/KPI/专项）" if intent.kpi_mode else "")
                + " → ## 下周计划 → ## 风险/阻塞。\n"
                "2. 【本周核心产出】每条写清“做了什么结果 + 业务价值/影响”，优先写可验证数字。\n"
                "3. 【项目进展】按「- **项目/专项**：已完成 / 进行中 / 待启动 — 关键里程碑」格式输出。\n"
                "4. 【下周计划】仅写可验收交付项，不写空泛动作词。\n"
                "5. 【风险/阻塞】写明影响范围与依赖。\n"
                + kpi_section_rule + "\n"
                f"以下是本周项目真实工作记录（共 {len(selected_contexts)} 条）：\n\n{context_text}"
                f"{quant_evidence_section}\n"
                "---\n"
                f"用户指令：{user_query}\n"
            )
        elif intent.task_type == "daily_report":
            prompt = (
                "【输出规则】\n"
                "1. 严格按照「用户指令」中要求的章节结构输出，没有数据支撑的章节直接跳过（含标题），不写'无相关内容'。\n"
                "2. 每条工作项格式：「- **动词短语**：详细描述」，动词短语是5字以内的动宾结构（如「完成了XX」「修复了XX」），不能用长句作标题。\n"
                "3. 描述直接以动词开头，省略所有人名，说明做了什么、为何重要、效果如何。\n"
                "4. 【今日小结】用段落格式（不用列表），不少于3句话。\n"
                "5. 禁止输出表格，禁止输出元数据标记。\n\n"
                f"以下是今日真实工作记录（共 {len(selected_contexts)} 条）：\n\n{context_text}\n\n"
                "---\n"
                f"用户指令：{user_query}\n"
            )
        elif intent.task_type == "project_summary":
            prompt = (
                f"以下是从本地数据库检索到的【项目工作记录】，共 {len(selected_contexts)} 条，"
                f"请严格基于这些记录生成项目总结报告：\n\n{context_text}\n\n"
                "---\n"
                "请严格按以下 Markdown 结构输出：\n\n"
                "## 项目背景与目标\n"
                "## 主要完成内容\n- 每条至少2句详细描述\n"
                "## 关键决策与方案\n"
                "## 遇到的挑战及解决方案\n"
                "## 成果与数据\n"
                "## 经验教训与改进建议\n"
                "## 下一步计划\n- 至少3条\n"
            )
        else:
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
            prompt = f"{link_rule}{floating_format_rule}工作记录上下文：\n{context_text}\n\n用户问题：{user_query}"

        # 根据意图动态选择 system prompt，并注入用户身份
        # user_identity 已在前面读取（用于主语去除），复用，避免重复查询 DB
        identity_clause = self._build_identity_clause(user_identity)
        if intent.task_type == "weekly_report":
            system = _WEEKLY_REPORT_SYSTEM_PROMPT + identity_clause
        elif intent.task_type == "project_weekly_report":
            system = _PROJECT_WEEKLY_REPORT_SYSTEM_PROMPT + identity_clause
        elif intent.task_type == "daily_report":
            system = _DAILY_REPORT_SYSTEM_PROMPT + identity_clause
        elif intent.task_type == "project_summary":
            system = _PROJECT_SUMMARY_SYSTEM_PROMPT + identity_clause
        else:
            system = self._system + identity_clause

        # 报告模式：明确要求 LLM 输出足够长的内容
        llm_kwargs = {}
        if is_report:
            # 报告类任务优先保证稳定返回，限制输出长度
            if intent.task_type == "weekly_report":
                llm_kwargs["num_predict"] = 768
            elif intent.task_type == "daily_report":
                llm_kwargs["num_predict"] = 640
            else:
                llm_kwargs["num_predict"] = 896
        elif "## 用户问题理解" in user_query and "## 回答" in user_query:
            llm_kwargs["num_predict"] = 8192
            llm_kwargs["temperature"] = 0.2
            llm_kwargs["top_p"] = 0.8

        primary_llm = llm or self._llm
        # 报告模式不再切换为硬编码的 qwen2.5:3b，避免 Ollama 频繁 swap 模型
        # 任何查询都使用当前激活的 LLM 模型

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

        # 报告模式：后处理兜底去除主语（应对 LLM 未遵从指令的情况）
        if is_report:
            answer = _postprocess_strip_subjects(answer, user_names)
        if _is_link_query(user_query):
            answer = _ensure_link_answer(answer, selected_contexts)
        if intent.task_type in ("weekly_report", "project_weekly_report"):
            answer = _normalize_evidence_references(answer, selected_contexts)
            answer = _normalize_weekly_report(answer)

        return RagResult(
            answer=answer,
            contexts=selected_contexts,
            model=llm_resp.model,
            tokens=llm_resp.tokens,
            done_reason=llm_resp.done_reason,
            output_truncated=_is_output_truncated(llm_resp.done_reason),
        )

    def _fetch_top_n_details(self, chunks: list[RetrievedChunk], top_n: int = 3) -> str:
        """对重要性最高的 top_n 条知识，从 DB 补充 details 字段，用于增强推理。
        报告模式下跳过 reading 类条目，避免文档查看类内容混入详情。
        """
        if not self._db_path:
            return ""
        # 过滤掉 reading 类，再按 importance 降序取 top_n
        filtered = [c for c in chunks if c.metadata.get("activity_type") != "reading"]
        sorted_chunks = sorted(
            filtered,
            key=lambda c: -(c.metadata.get("importance") or 3),
        )[:top_n]
        parts = []
        try:
            conn = sqlite3.connect(self._db_path)
            for chunk in sorted_chunks:
                knowledge_id = chunk.metadata.get("knowledge_id")
                if not knowledge_id:
                    continue
                row = conn.execute(
                    "SELECT overview, details FROM timelines WHERE id = ?",
                    (knowledge_id,),
                ).fetchone()
                if not row:
                    continue
                overview, details = row
                if details and details.strip():
                    header = overview or chunk.text[:60]
                    parts.append(f"- 【{header}】\n  详情：{details.strip()[:1000]}")
            conn.close()
        except Exception as exc:
            logger.warning("补充 knowledge details 失败: %s", exc)
        return "\n".join(parts)

    def _build_quant_evidence_block(
        self,
        chunks: list[RetrievedChunk],
        kpi_mode: bool = False,
        top_n: int = 6,
    ) -> str:
        """从候选知识中提炼可验证的量化事实，供周报类提示词强约束引用。"""
        if not chunks:
            return ""

        details_map = self._load_knowledge_details(chunks)
        candidates: list[tuple[str, str, float]] = []

        for context_index, chunk in enumerate(chunks, 1):
            metadata = chunk.metadata or {}
            knowledge_id = metadata.get("knowledge_id")
            text_parts = [(chunk.text or "").strip()]
            if knowledge_id is not None:
                try:
                    kid = int(knowledge_id)
                    detail_text = details_map.get(kid)
                    if detail_text:
                        text_parts.append(detail_text)
                except Exception:
                    pass

            fact_lines = self._extract_quant_fact_lines("\n".join(text_parts), kpi_mode=kpi_mode)
            if not fact_lines:
                continue

            evidence_ref = f"R#{context_index}"
            evidence_score = self._score_evidence(metadata)
            for fact in fact_lines:
                candidates.append((fact, evidence_ref, evidence_score))

        if not candidates:
            return ""

        dedup: dict[str, tuple[str, str, float]] = {}
        for fact, ref, score in candidates:
            key = self._normalize_fact_key(fact)
            prev = dedup.get(key)
            if prev is None or score > prev[2]:
                dedup[key] = (fact, ref, score)

        ranked = sorted(dedup.values(), key=lambda item: (-item[2], len(item[0])))
        picked = ranked[: max(1, top_n)]

        lines = ["【量化证据】（仅可引用以下证据中的数字结论）"]
        for idx, (fact, ref, _) in enumerate(picked, 1):
            lines.append(f"- [{idx}] {fact}（证据：{ref}）")
        return "\n".join(lines)

    def _load_knowledge_details(self, chunks: list[RetrievedChunk]) -> dict[int, str]:
        """按 knowledge_id 批量加载详情文本，避免逐条查询。"""
        if not self._db_path:
            return {}

        knowledge_ids: list[int] = []
        seen: set[int] = set()
        for chunk in chunks:
            knowledge_id = (chunk.metadata or {}).get("knowledge_id")
            if knowledge_id is None:
                continue
            try:
                kid = int(knowledge_id)
            except Exception:
                continue
            if kid in seen:
                continue
            seen.add(kid)
            knowledge_ids.append(kid)

        if not knowledge_ids:
            return {}

        placeholders = ",".join("?" for _ in knowledge_ids)
        details_map: dict[int, str] = {}
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                f"SELECT id, details FROM timelines WHERE id IN ({placeholders})",
                knowledge_ids,
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    kid = int(row[0])
                except Exception:
                    continue
                details = (row[1] or "").strip()
                if details:
                    details_map[kid] = details[:1200]
        except Exception as exc:
            logger.warning("批量读取 knowledge details 失败: %s", exc)

        return details_map

    @staticmethod
    def _extract_quant_fact_lines(text: str, kpi_mode: bool = False) -> list[str]:
        if not text:
            return []

        progress_keywords = (
            "完成", "达成", "推进", "上线", "交付", "修复", "关闭", "处理", "新增", "减少", "降低", "提升", "优化",
            "通过率", "成功率", "失败率", "耗时", "时延", "里程碑", "okr", "kpi", "专项", "progress", "improve", "fixed", "delivered",
        )
        number_pattern = re.compile(
            r"(\d+(?:\.\d+)?\s*%|\d+\s*/\s*\d+|\d+(?:\.\d+)?\s*(?:个|项|次|处|条|页|分钟|小时|天|周|月|年|ms|s|秒|模块|接口|问题|bug|任务|需求|pr|PR|commit|人天|台|条告警))"
        )

        candidates: list[str] = []
        segments = re.split(r"[\n。；;！？!?]+", text)
        for seg in segments:
            line = " ".join(seg.strip().split())
            if len(line) < 6:
                continue
            if not number_pattern.search(line):
                continue

            lowered = line.lower()
            if not any((kw in line) or (kw in lowered) for kw in progress_keywords):
                continue
            if RagPipeline._looks_like_noise_numeric_line(line):
                continue
            if kpi_mode and not any(token in lowered for token in ("okr", "kpi", "专项", "达成", "完成", "提升", "降低", "上线", "交付", "通过率")):
                # KPI 模式更严格，尽量保留“进展结论”而非普通数字描述
                continue

            candidates.append(line[:120])

        return candidates[:8]

    @staticmethod
    def _looks_like_noise_numeric_line(line: str) -> bool:
        if re.fullmatch(r"[\d\s\-/:年月日.]+", line):
            return True

        has_progress_word = any(
            token in line for token in ("完成", "达成", "提升", "下降", "减少", "增加", "修复", "关闭", "交付", "上线", "通过率", "耗时")
        )
        if re.search(r"\b20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2})?", line) and not has_progress_word:
            return True
        if re.search(r"\bv?\d+\.\d+\.\d+\b", line) and not has_progress_word:
            return True

        return False

    @staticmethod
    def _normalize_fact_key(fact: str) -> str:
        normalized = fact.lower()
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r"[，,。；;：:（）()\[\]【】'\"]", "", normalized)
        return normalized

    @staticmethod
    def _format_evidence_ref(metadata: dict, capture_id: int) -> str:
        knowledge_id = metadata.get("knowledge_id")
        capture = metadata.get("capture_id") or capture_id

        knowledge_ref = None
        capture_ref = None
        try:
            if knowledge_id is not None:
                knowledge_ref = f"K#{int(knowledge_id)}"
        except Exception:
            knowledge_ref = None

        try:
            if capture is not None:
                capture_ref = f"C#{int(capture)}"
        except Exception:
            capture_ref = None

        if knowledge_ref and capture_ref:
            return f"{knowledge_ref}/{capture_ref}"
        if knowledge_ref:
            return knowledge_ref
        if capture_ref:
            return capture_ref

        doc_key = str(metadata.get("doc_key") or "").strip()
        return f"DOC#{doc_key}" if doc_key else "未知证据"

    @staticmethod
    def _score_evidence(metadata: dict) -> float:
        evidence_strength = str(metadata.get("evidence_strength") or "").lower()
        strength_score = {"high": 1.6, "medium": 1.0, "low": 0.2}.get(evidence_strength, 0.5)

        try:
            importance = float(metadata.get("importance") or 3)
        except Exception:
            importance = 3.0
        importance_score = max(0.0, min(importance, 5.0)) * 0.25

        user_verified_score = 2.0 if metadata.get("user_verified") else 0.0

        ts_value = metadata.get("observed_at") or metadata.get("time") or metadata.get("end_time") or metadata.get("start_time")
        recency_score = 0.0
        try:
            ts_int = int(ts_value)
            age_days = (int(time.time() * 1000) - ts_int) / (24 * 60 * 60 * 1000)
            recency_score = max(0.0, 1.2 - age_days / 14)
        except Exception:
            recency_score = 0.0

        return user_verified_score + strength_score + importance_score + recency_score

    @staticmethod
    def _build_context(chunks: list[RetrievedChunk], strip_user_subject: bool = False, user_names: Optional[list[str]] = None) -> str:
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
            text = chunk.text[:_MAX_CHUNK_LEN]
            if strip_user_subject:
                text = _strip_report_metadata(text)
                text = _strip_user_subject(text, user_names=user_names)
                # 报告模式下不向 LLM 暴露看到时间/来源/活动等元数据，避免污染正式输出
                parts.append(f"[{i}] {text}")
                continue
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
            # importance 仅作内部排序依据，不放入上下文文本（避免 LLM 在输出中暴露元数据）
            parts.append(f"{' '.join(prefix)} {text}")
        return "\n\n".join(parts)

    @staticmethod
    def _select_contexts(chunks: list[RetrievedChunk], top_k: int, query_mode: str = "lookup") -> list[RetrievedChunk]:
        selected: list[RetrievedChunk] = []
        selected_keys: set[str] = set()

        is_report_mode = query_mode == "summary"
        logger.info(f"_select_contexts: 输入 {len(chunks)} 条，top_k={top_k}, query_mode={query_mode}")

        candidate_chunks = sorted(
            chunks,
            key=lambda chunk: (
                # 报告模式：importance 低的排后面（importance 为 None 按 3 处理）
                -(chunk.metadata.get("importance") or 3) if is_report_mode else 0,
                -_retrieval_sort_score(chunk) if not is_report_mode else 0,
                _source_priority(chunk.metadata.get("source_type") or chunk.source) if not is_report_mode else 0,
                0 if is_report_mode and chunk.metadata.get("activity_type") not in {"other", None} else 1,
                0 if is_report_mode and chunk.metadata.get("evidence_strength") in {"high", "medium"} else 1,
                -float(chunk.score) if is_report_mode else 0,
            ),
        )

        for chunk in candidate_chunks:
            if len(selected) >= top_k:
                break
            source_type = chunk.metadata.get("source_type") or chunk.source
            if source_type not in {"knowledge", "document", "pending_document", "bake_knowledge", "operation"}:
                logger.info(f"  跳过: source_type={source_type}")
                continue
            if _is_noise_chunk(chunk):
                logger.info(f"  跳过: 噪音chunk")
                continue
            # 报告模式下直接丢弃 importance=1 的极低价值记录
            if is_report_mode and (chunk.metadata.get("importance") or 3) <= 1:
                logger.info(f"  跳过: importance={chunk.metadata.get('importance')} <= 1")
                continue

            activity_type = chunk.metadata.get("activity_type")
            evidence_strength = chunk.metadata.get("evidence_strength")
            importance = chunk.metadata.get("importance") or 3
            history_view = chunk.metadata.get("history_view")

            # 报告模式下的精细过滤
            if is_report_mode:
                # 1. 过滤低可信度记录（除非重要性很高）
                if evidence_strength == "low" and importance < 4:
                    logger.info(f"  跳过: evidence_strength=low且importance={importance} < 4")
                    continue

                # 2. 过滤不相关的活动类型
                if activity_type not in ("coding", "writing", "reading", "meeting", "chat", "ask_ai", "reviewing_history", None):
                    logger.info(f"  跳过: activity_type={activity_type}不在白名单")
                    continue

                # 3. reading 类活动需要很高的重要性
                if activity_type == "reading" and importance < 4:
                    logger.info(f"  跳过: reading且importance={importance} < 4")
                    continue

                # 4. 回看历史产生的知识需要较高重要性
                if history_view and importance < 3:
                    logger.info(f"  跳过: history_view=True且importance={importance} < 3")
                    continue

                # 5. writing/reviewing_history 需要中等重要性
                if activity_type in ("writing", "reviewing_history") and importance < 3:
                    logger.info(f"  跳过: {activity_type}且importance={importance} < 3")
                    continue

            # 报告模式下，activity_type 为空且 overview 中含典型"查看"行为描述的记录过滤掉
            if is_report_mode and not activity_type:
                if any(kw in chunk.text for kw in ("在查看", "在浏览", "在阅读", "查看了", "浏览了", "阅读了")):
                    logger.info(f"  跳过: activity_type为空且包含查看关键词")
                    continue

            doc_key = chunk.doc_key or chunk.metadata.get("doc_key")
            if not doc_key or doc_key in selected_keys:
                logger.info(f"  跳过: doc_key重复或为空")
                continue
            if source_type == "knowledge" and any(
                str(chunk.metadata.get("knowledge_id")) in _source_ids_of(selected_chunk)
                for selected_chunk in selected
            ):
                logger.info("  跳过: 时间线已有对应产物入选")
                continue
            logger.info(f"  ✓ 选中: importance={chunk.metadata.get('importance')}, activity={chunk.metadata.get('activity_type')}")
            selected.append(chunk)
            selected_keys.add(doc_key)
            for linked_id in _source_ids_of(chunk):
                selected_keys.add(f"knowledge:{linked_id}")

        logger.info(f"_select_contexts: 最终选中 {len(selected)} 条")
        return selected

    @staticmethod
    def _parse_query_intent(user_query: str) -> QueryIntent:
        lowered = user_query.lower()
        now_ms = int(time.time() * 1000)
        start_ts: Optional[int] = None
        end_ts: Optional[int] = now_ms
        observed_start_ts: Optional[int] = None
        observed_end_ts: Optional[int] = None
        event_start_ts: Optional[int] = None
        event_end_ts: Optional[int] = None
        target_time_semantics = "either"
        task_type: Optional[str] = None

        # ── 任务型意图检测（优先级最高）──────────────────────────────────
        _PROJECT_WEEKLY_REPORT_TOKENS = (
            "项目周报", "本周项目周报", "项目进展周报", "本周项目进展", "project weekly", "project weekly report"
        )
        _WEEKLY_REPORT_TOKENS  = ("周报", "工作周报", "weekly report")
        _DAILY_REPORT_TOKENS   = ("日报", "工作日报", "今日工作总结", "今天工作总结", "daily report", "工作日记", "今日日记")
        _PROJECT_SUMMARY_TOKENS = ("项目总结", "项目报告", "项目复盘", "项目回顾", "project summary", "项目里程碑", "milestone")
        _WRITE_TASK_TOKENS     = ("帮我写", "帮我生成", "帮我整理", "生成一份", "写一份", "整理一份", "写下", "生成下", "帮忙写", "帮我做", "生成")

        _is_project_weekly_report = any(t in lowered for t in _PROJECT_WEEKLY_REPORT_TOKENS)
        _is_weekly_report   = any(t in lowered for t in _WEEKLY_REPORT_TOKENS)
        _is_daily_report    = any(t in lowered for t in _DAILY_REPORT_TOKENS)
        _is_project_summary = any(t in lowered for t in _PROJECT_SUMMARY_TOKENS)
        _is_write_intent    = any(t in lowered for t in _WRITE_TASK_TOKENS)

        kpi_mode = any(token in lowered for token in (
            "okr", "kpi", "专项", "关键结果", "指标", "里程碑", "达成率", "完成率"
        ))

        # 报告类意图：只要含报告关键词即触发，无需额外的"帮我写"前缀
        if _is_project_weekly_report:
            task_type = "project_weekly_report"
        elif _is_project_summary:
            task_type = "project_summary"
        elif _is_weekly_report:
            task_type = "weekly_report"
        elif _is_daily_report:
            task_type = "daily_report"

        # ── 时间范围解析 ─────────────────────────────────────────────────
        if "上周" in user_query:
            # 上周：上周一 00:00 ~ 本周一 00:00 - 1ms
            this_week_start = _week_start_ms()
            start_ts = this_week_start - 7 * 24 * 60 * 60 * 1000
            end_ts = this_week_start - 1
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif "最近" in user_query:
            start_ts = now_ms - 7 * 24 * 60 * 60 * 1000
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif "今天" in user_query:
            start_ts = _day_start_ms(0)
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif "昨天" in user_query:
            start_ts = _day_start_ms(-1)
            end_ts = _day_start_ms(0) - 1
            observed_start_ts = start_ts
            observed_end_ts = end_ts
            event_start_ts = start_ts
            event_end_ts = end_ts
        elif "本周" in user_query:
            start_ts = _week_start_ms()
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif task_type in ("weekly_report", "project_weekly_report"):
            # 周报默认取本周
            start_ts = _week_start_ms()
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif task_type == "daily_report":
            # 日报默认取今天
            start_ts = _day_start_ms(0)
            observed_start_ts = start_ts
            observed_end_ts = end_ts
        elif task_type == "project_summary":
            # 项目总结默认取本月
            start_ts = _month_start_ms()
            observed_start_ts = start_ts
            observed_end_ts = end_ts

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
        query_mode = "lookup"

        # ── 任务型意图：统一设置检索参数 ─────────────────────────────────
        if task_type in ("weekly_report", "daily_report", "project_weekly_report"):
            target_time_semantics = "observed"
            history_view = None  # 检索阶段不过滤，在 _select_contexts 中精细筛选
            activity_types = []  # 检索阶段不过滤，在 _select_contexts 中精细筛选
            evidence_strengths = []  # 检索阶段不过滤，在 _select_contexts 中精细筛选
            query_mode = "summary"
        else:
            # ── 普通查询意图 ──────────────────────────────────────────────
            asks_ai = any(token in lowered for token in ("gemini", "claude", "chatgpt", "ai")) and any(
                token in user_query for token in ("问", "提问", "聊", "对话")
            )
            asks_history = any(token in user_query for token in ("历史消息", "历史记录", "历史对话", "回看", "回顾"))
            asks_daily_summary = "今天" in user_query and any(token in user_query for token in ("做了什么", "干了什么", "做过什么"))
            asks_recent_summary = any(token in user_query for token in ("最近", "本周", "上周")) and any(
                token in user_query for token in ("关于", "工作有哪些", "工作内容", "进展", "总结", "做了哪些", "回顾", "汇总", "梳理", "有什么")
            )

            if asks_ai:
                target_time_semantics = "observed"
                activity_types = ["ask_ai"]
                history_view = False
                evidence_strengths = ["medium", "high"]
            elif asks_history:
                target_time_semantics = "observed"
                activity_types = ["reviewing_history", "chat", "reading"]
                content_origins = ["historical_content"]
                history_view = True
                evidence_strengths = ["medium", "high"]
            elif asks_daily_summary:
                target_time_semantics = "observed"
                history_view = False
                activity_types = ["coding", "reading", "meeting", "chat", "ask_ai"]
                evidence_strengths = ["medium", "high"]
                query_mode = "summary"
            elif asks_recent_summary:
                target_time_semantics = "observed"
                history_view = False
                activity_types = ["coding", "reading", "meeting", "chat", "ask_ai"]
                evidence_strengths = ["medium", "high"]
                query_mode = "summary"

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
            query_mode=query_mode,
            activity_types=activity_types,
            content_origins=content_origins,
            history_view=history_view,
            is_self_generated=is_self_generated,
            evidence_strengths=evidence_strengths,
            task_type=task_type,
            kpi_mode=kpi_mode,
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


def _retrieval_sort_score(chunk: RetrievedChunk) -> float:
    metadata = chunk.metadata or {}
    score = metadata.get("retrieval_score")
    try:
        return float(score)
    except (TypeError, ValueError):
        return float(chunk.score or 0)


def _append_missing_artifact_candidates(
    merged: list[RetrievedChunk],
    candidates: list[RetrievedChunk],
    limit: int,
) -> list[RetrievedChunk]:
    """Keep high-confidence bake artifacts visible after RRF truncation.

    Keyword artifact search already ranks documents/knowledge/SOPs by title and body matches.
    When vector results dominate the RRF top slice, a directly matched document can be dropped
    before _select_contexts has a chance to apply source priority.
    """
    if len(merged) >= limit:
        return merged

    seen = {chunk.doc_key or (chunk.metadata or {}).get("doc_key") for chunk in merged}
    appended: list[RetrievedChunk] = []
    for chunk in candidates:
        source_type = (chunk.metadata or {}).get("source_type") or chunk.source
        # Legacy timeline knowledge commonly carries a normalized score below 1.0 and is
        # still a primary, directly matched source. Bake artifacts use FTS-style scores;
        # only append those when the keyword signal is strong so weak document matches do
        # not leak back after the vector-aware RRF threshold filtered them out.
        is_primary_knowledge = source_type == "knowledge"
        is_strong_bake_artifact = (
            source_type in {"document", "bake_knowledge", "operation"}
            and float(chunk.score or 0) >= 5.0
        )
        if not is_primary_knowledge and not is_strong_bake_artifact:
            continue
        doc_key = chunk.doc_key or (chunk.metadata or {}).get("doc_key")
        if not doc_key or doc_key in seen:
            continue
        appended.append(chunk)
        seen.add(doc_key)
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
            "docs.corp.",
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


def _source_priority(source_type: Optional[str]) -> int:
    return {
        "document": 0,
        "pending_document": 1,
        "bake_knowledge": 2,
        "operation": 3,
        "knowledge": 4,
    }.get(str(source_type or ""), 9)


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



def _postprocess_strip_subjects(text: str, user_names: list[str]) -> str:
    """对 LLM 最终输出做后处理，去掉人名和第三人称代词作主语。"""
    import re
    if not text:
        return text

    name_parts = ["用户"]
    for name in (user_names or []):
        if name:
            name_parts.append(re.escape(name))
    subject_re = "|".join(name_parts)

    # 实义动词：去掉名字后保留
    verb_prefix = "使用|完成|开始|查看|讨论|编辑|设计|生成|发送|参与|调用|更新|优化|实现|修复|确认|选择|提出|决定|负责|进行|尝试|整理|分析|构建|调试|部署|配置|在|对|与|通过|于|并|已|正在"

    lines = text.split("\n")
    result = []
    for line in lines:
        # 1. 冒号后「人名」→ 只去名字，保留后续所有词（含介词/动词）
        line = re.sub(rf"([:：])(?:{subject_re})", r"\1", line)
        # 2. 逗号后夹入型「，人名」→ 去名字
        line = re.sub(rf"，(?:{subject_re})", "，", line)
        # 3. 行首或句首人名 → 去掉名字（保留后续词）
        line = re.sub(rf"^(?:{subject_re})", "", line.lstrip())
        # 4. 句中任意位置残留人名紧跟动词/介词 → 去名字
        line = re.sub(rf"(?:{subject_re})(?=(?:{verb_prefix}))", "", line)
        # 5. 他/她作主语 → 去掉
        line = re.sub(rf"(?<![的地得])(?:他|她)(?:们)?(?=(?:{verb_prefix}))", "", line)
        # 6. 清理标点后多余空格
        line = re.sub(r"([:：])\s+", r"\1", line)
        result.append(line)
    return "\n".join(result)


def _normalize_evidence_references(text: str, contexts: list[RetrievedChunk]) -> str:
    """把内部证据号归一为前端参考资料序号，避免 answer 引用到不可对应的 K#/C#。"""
    if not text or not contexts:
        return text

    replacements: dict[str, str] = {}
    for index, chunk in enumerate(contexts, 1):
        metadata = chunk.metadata or {}
        display_ref = f"R#{index}"
        internal_ref = RagPipeline._format_evidence_ref(metadata, chunk.capture_id)
        if internal_ref and internal_ref != "未知证据":
            replacements[internal_ref] = display_ref

        knowledge_id = metadata.get("knowledge_id")
        capture_id = metadata.get("capture_id") or chunk.capture_id
        try:
            if knowledge_id is not None:
                replacements[f"K#{int(knowledge_id)}"] = display_ref
        except Exception:
            pass
        try:
            if capture_id is not None:
                replacements[f"C#{int(capture_id)}"] = display_ref
        except Exception:
            pass

    normalized = text
    for old_ref in sorted(replacements, key=len, reverse=True):
        normalized = normalized.replace(old_ref, replacements[old_ref])
    return normalized


def _normalize_weekly_report(text: str) -> str:
    """对周报结果做轻量格式修正，提升小模型输出稳定性。"""
    import re

    if not text:
        return text

    text = text.replace("### ", "## ")
    lines = [line.rstrip() for line in text.split("\n")]

    # 丢弃模型回显的原始工作记录或内部证据清单。
    cut_markers = (
        "工作记录：",
        "以下是本周真实工作记录",
        "原始工作记录：",
        "依据证据",
        "证据依据",
        "引用依据",
        "参考依据",
        "【量化证据】",
        "量化证据：",
    )
    trimmed = []
    for line in lines:
        if any(marker in line for marker in cut_markers):
            break
        trimmed.append(line)
    lines = trimmed

    # 删除占位词
    cleaned = []
    placeholder_patterns = (
        r"^[-*]?\s*无相关内容[。.]?$",
        r"^[-*]?\s*暂无相关内容[。.]?$",
        r"^[-*]?\s*暂无[。.]?$",
        r"^[-*]?\s*无[。.]?$",
        r"^[-*]?\s*暂无相关风险[。.]?$",
        r"^[-*]?\s*无相关风险[。.]?$",
        r"^[-*]?\s*暂无风险[。.]?$",
        r"^[-*]?\s*无风险[。.]?$",
        r"^[-*]?\s*暂无阻塞[。.]?$",
        r"^[-*]?\s*无阻塞[。.]?$",
    )
    for line in lines:
        line = line.replace("具体可交付目标：", "")
        line = line.replace("项目名：", "")
        line = line.replace("短标题：", "")
        line = line.replace("详细描述：", "")
        line = line.replace("风险点：", "")
        if any(re.match(pattern, line.strip()) for pattern in placeholder_patterns):
            continue
        cleaned.append(line.rstrip())
    lines = cleaned

    section_aliases = {
        "本周核心产出": "## 本周核心产出",
        "项目进展": "## 项目进展",
        "下周计划": "## 下周计划",
        "风险/阻塞": "## 风险/阻塞",
        "风险阻塞": "## 风险/阻塞",
    }

    normalized = []
    current_section = None
    last_bullet_idx = None

    def _ensure_section(section_name: str):
        nonlocal current_section
        header = section_aliases[section_name]
        if current_section != header:
            if normalized and normalized[-1] != "":
                normalized.append("")
            normalized.append(header)
            current_section = header

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # 标题归一化
        matched_header = None
        for alias, header in section_aliases.items():
            if line == header or line == alias or line == f"- **{alias}**：" or line.startswith(f"{alias}："):
                matched_header = alias
                break
        if matched_header:
            _ensure_section(matched_header)
            last_bullet_idx = None
            continue

        # “重要性：”并到上一条 bullet
        if line.startswith(("重要性：", "- 重要性：", "**重要性**：")) and last_bullet_idx is not None:
            extra = re.sub(r"^(?:-\s*)?(?:\*\*重要性\*\*：|重要性：)", "", line).strip()
            normalized[last_bullet_idx] = normalized[last_bullet_idx].rstrip() + f" {extra}"
            continue

        # 如果还没进入任何章节，根据内容猜测章节
        if current_section is None:
            if "已完成" in line or "进行中" in line or "待启动" in line:
                _ensure_section("项目进展")
            elif any(k in line for k in ("风险", "阻塞", "受阻")):
                _ensure_section("风险/阻塞")
            else:
                _ensure_section("本周核心产出")

        # 项目进展中，把散行转成 bullet，并跳过空洞占位内容
        if current_section == "## 项目进展":
            if re.fullmatch(r"(?:无相关内容|暂无相关内容|暂无|无)[。.]?", line):
                continue
            if not line.startswith("-"):
                line = f"- {line}"
            normalized.append(line)
            last_bullet_idx = len(normalized) - 1
            continue

        # 下周计划/风险阻塞统一 bullet，并跳过空洞占位内容
        if current_section in ("## 下周计划", "## 风险/阻塞"):
            if re.fullmatch(r"(?:无相关内容|暂无相关内容|暂无|无|暂无相关风险|无相关风险|暂无风险|无风险|暂无阻塞|无阻塞)[。.]?", line):
                continue
            if not line.startswith("-"):
                line = f"- {line}"

        normalized.append(line)
        last_bullet_idx = len(normalized) - 1 if line.startswith("-") else None

    # 删除空章节
    result = []
    i = 0
    while i < len(normalized):
        line = normalized[i]
        if line.startswith("## "):
            j = i + 1
            has_content = False
            while j < len(normalized) and not normalized[j].startswith("## "):
                candidate = normalized[j].strip()
                if candidate and candidate != "-" and not re.fullmatch(r"-?\s*(?:无相关内容|暂无相关内容|暂无|无|暂无相关风险|无相关风险|暂无风险|无风险|暂无阻塞|无阻塞)[。.]?", candidate):
                    has_content = True
                    break
                j += 1
            if has_content:
                if result and result[-1] != "":
                    result.append("")
                result.append(line)
            i += 1
            continue
        if result and result[-1].startswith("## ") and line == "":
            i += 1
            continue
        if line.strip() and line.strip() != "-":
            result.append(line)
        i += 1

    return "\n".join(result).strip()


def _strip_report_metadata(text: str) -> str:
    """报告模式下清理知识条目中的元数据行，只保留概述/详情正文。"""
    lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith((
            "看到时间：", "记录时间：", "事件时间：", "时长：", "应用：", "窗口：",
            "活动类型：", "内容来源：", "重要性：", "来源："
        )):
            continue
        if line.startswith("概述："):
            lines.append(line[len("概述："):].strip())
            continue
        if line.startswith("详情："):
            lines.append(line[len("详情："):].strip())
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_user_subject(text: str, user_names: Optional[list[str]] = None) -> str:
    """从知识条目文本中去掉多余的人物主语（用户/姓名/他/她），适用于报告生成。"""
    import re
    lines = text.split("\n")
    result = []

    verb_prefix = "使用|完成|开始|查看|讨论|编辑|设计|生成|发送|参与|调用|更新|优化|实现|修复|进行|尝试|整理|分析|构建|调试|部署|配置|在|对|与|通过|于|并|已|继续|打开|运行|执行|创建|删除|修改|处理|获取|请求|提交|确认|检查|测试|发现|遇到|解决|正在|切换|记录|还|又|也|提出|建议|反馈|询问|回复|表示|指出|认为|说|要求|决定|确定|选择|发起|负责|主导"

    name_pattern_parts = ["用户"]
    if user_names:
        for name in user_names:
            if name:
                name_pattern_parts.append(re.escape(name))
    subject_re = "|".join(name_pattern_parts)

    for line in lines:
        line = re.sub(rf"(概述：|详情：)(?:{subject_re})", r"\1", line)
        line = re.sub(rf"，(?:{subject_re})", "，", line)
        line = re.sub(rf"^(?:{subject_re})", "", line.lstrip())
        line = re.sub(rf"(?:{subject_re})(?=(?:{verb_prefix}))", "", line)
        line = re.sub(rf"(?<![的地得])(?:他|她)(?:们)?(?=(?:{verb_prefix}))", "", line)
        result.append(line)
    return "\n".join(result)


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


def _month_start_ms() -> int:
    now = time.localtime()
    month_start = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    return int(month_start * 1000)



def _format_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))
    except Exception:
        return str(ts)
