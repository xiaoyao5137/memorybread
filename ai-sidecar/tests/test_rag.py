"""
RAG 模块测试

测试覆盖：
- RetrievedChunk 数据类属性
- reciprocal_rank_fusion (RRF) 合并逻辑
- RagPipeline.query()（正常 / 无上下文 / embedding 失败降级）
- VectorRetriever.is_available()（不依赖 Qdrant 安装）
- LLM 后端接口验证（MockLlmBackend / OllamaBackend 可用性检测）
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

import pytest

from embedding.base  import EmbeddingBackend, EmbeddingVector
from embedding.model import EmbeddingModel
from rag.llm.base    import LlmBackend, LlmResponse
from rag.llm.ollama  import OllamaBackend
from rag.pipeline    import (
    RagPipeline,
    RagResult,
    _attach_document_links,
    _collect_document_links,
    _lookup_baked_mention,
    _normalize_doc_title,
)
from rag.query_planner import build_artifact_query_plan
from rag.retriever   import (
    Fts5Retriever,
    KnowledgeFts5Retriever,
    RetrievedChunk,
    VectorRetriever,
    VectorSearchFilter,
    _bounded_fallback_terms,
    _consecutive_query_phrases,
    _phrase_boost,
    _phrase_present,
)
from rag.reranker    import reciprocal_rank_fusion


# ── Mock 工具 ─────────────────────────────────────────────────────────────────

class MockEmbeddingBackend(EmbeddingBackend):
    def __init__(self, dim: int = 4, should_raise: Optional[Exception] = None) -> None:
        self._dim = dim
        self._should_raise = should_raise

    def is_available(self) -> bool:
        return True

    def encode(self, texts: list[str]) -> list[EmbeddingVector]:
        if self._should_raise:
            raise self._should_raise
        return [EmbeddingVector(text=t, vector=[0.1] * self._dim) for t in texts]

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def dimension(self) -> int:
        return self._dim


class MockLlmBackend(LlmBackend):
    def __init__(
        self,
        response: str = "模拟回答",
        available: bool = True,
        model_name: str = "mock-llm",
        done_reason: Optional[str] = None,
    ) -> None:
        self._response  = response
        self._available = available
        self._model_name = model_name
        self._done_reason = done_reason
        self.call_count = 0
        self.last_prompt: str = ""
        self.last_system: str = ""
        self.last_kwargs: dict = {}

    def is_available(self) -> bool:
        return self._available

    def complete(self, prompt: str, system: str = "", **kwargs) -> LlmResponse:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system = system
        self.last_kwargs = kwargs
        return LlmResponse(text=self._response, model=self._model_name, tokens=10, done_reason=self._done_reason)

    @property
    def model_name(self) -> str:
        return self._model_name


class MockFts5Retriever:
    """鸭子类型 Fts5Retriever（无需 SQLite 连接）"""
    def __init__(self, chunks: Optional[list[RetrievedChunk]] = None) -> None:
        self._chunks    = chunks or []
        self.call_count = 0
        self.last_kwargs: dict = {}

    def search(
        self,
        query: str,
        top_k: int = 10,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        entity_terms: Optional[list[str]] = None,
        observed_start_ts: Optional[int] = None,
        observed_end_ts: Optional[int] = None,
        event_start_ts: Optional[int] = None,
        event_end_ts: Optional[int] = None,
        activity_types: Optional[list[str]] = None,
        content_origins: Optional[list[str]] = None,
        history_view: Optional[bool] = None,
        is_self_generated: Optional[bool] = None,
        evidence_strengths: Optional[list[str]] = None,
        created_start_ts: Optional[int] = None,
        created_end_ts: Optional[int] = None,
    ) -> list[RetrievedChunk]:
        self.call_count += 1
        self.last_kwargs = {
            "query": query,
            "top_k": top_k,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "entity_terms": entity_terms,
            "observed_start_ts": observed_start_ts,
            "observed_end_ts": observed_end_ts,
            "event_start_ts": event_start_ts,
            "event_end_ts": event_end_ts,
            "activity_types": activity_types,
            "content_origins": content_origins,
            "history_view": history_view,
            "is_self_generated": is_self_generated,
            "evidence_strengths": evidence_strengths,
            "created_start_ts": created_start_ts,
            "created_end_ts": created_end_ts,
        }
        return self._chunks[:top_k]


class MockVectorRetriever:
    """鸭子类型 VectorRetriever（无需 Qdrant 连接）"""
    def __init__(
        self,
        chunks:    Optional[list[RetrievedChunk]] = None,
        available: bool = True,
    ) -> None:
        self._chunks    = chunks or []
        self._available = available
        self.call_count = 0
        self.last_kwargs: dict = {}

    def is_available(self) -> bool:
        return self._available

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        score_threshold: float = 0.3,
        filters: Optional[VectorSearchFilter] = None,
    ) -> list[RetrievedChunk]:
        self.call_count += 1
        self.last_kwargs = {
            "query_vector": query_vector,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "filters": filters,
        }
        return self._chunks[:top_k]

    def upsert(self, *args, **kwargs) -> bool:
        return True


def _chunk(
    cid: int,
    score: float = 0.5,
    source: str = "fts5",
    doc_key: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        capture_id=cid,
        text=f"内容-{cid}",
        score=score,
        source=source,
        doc_key=doc_key,
        metadata=metadata,
    )


def _make_pipeline(
    fts_chunks: Optional[list[RetrievedChunk]] = None,
    knowledge_chunks: Optional[list[RetrievedChunk]] = None,
    vector_chunks: Optional[list[RetrievedChunk]] = None,
    llm_response: str = "测试回答",
    embed_raise: Optional[Exception] = None,
    top_k: int = 3,
) -> RagPipeline:
    return RagPipeline(
        embedding_model=EmbeddingModel(backend=MockEmbeddingBackend(should_raise=embed_raise)),
        vector_retriever=MockVectorRetriever(chunks=vector_chunks),  # type: ignore[arg-type]
        fts5_retriever=MockFts5Retriever(chunks=fts_chunks),  # type: ignore[arg-type]
        knowledge_retriever=MockFts5Retriever(chunks=knowledge_chunks),  # type: ignore[arg-type]
        llm=MockLlmBackend(response=llm_response),
        top_k=top_k,
    )


def _init_captures_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL,
            app_name TEXT,
            win_title TEXT,
            url TEXT,
            webpage_title TEXT,
            ocr_text TEXT,
            ax_text TEXT,
            input_text TEXT,
            audio_text TEXT
        );
        CREATE VIRTUAL TABLE captures_fts USING fts5(
            ax_text,
            ocr_text,
            input_text,
            audio_text,
            content='captures',
            content_rowid='id'
        );
        CREATE TRIGGER captures_fts_insert AFTER INSERT ON captures BEGIN
            INSERT INTO captures_fts(rowid, ax_text, ocr_text, input_text, audio_text)
            VALUES (new.id, new.ax_text, new.ocr_text, new.input_text, new.audio_text);
        END;
        """
    )
    conn.commit()
    conn.close()



def _init_knowledge_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY,
            capture_id INTEGER NOT NULL,
            summary TEXT,
            overview TEXT,
            details TEXT,
            start_time INTEGER,
            end_time INTEGER,
            duration_minutes INTEGER,
            frag_app_name TEXT,
            frag_win_title TEXT,
            entities TEXT,
            category TEXT,
            user_verified INTEGER DEFAULT 0,
            observed_at INTEGER,
            event_time_start INTEGER,
            event_time_end INTEGER,
            history_view INTEGER DEFAULT 0,
            content_origin TEXT,
            activity_type TEXT,
            is_self_generated INTEGER DEFAULT 0,
            evidence_strength TEXT,
            importance INTEGER DEFAULT 3,
            created_at_ms INTEGER
        );
        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            overview,
            details,
            entities,
            content='timelines',
            content_rowid='id'
        );
        CREATE TRIGGER knowledge_ai AFTER INSERT ON timelines BEGIN
            INSERT INTO knowledge_fts(rowid, overview, details, entities)
            VALUES (new.id, new.overview, new.details, new.entities);
        END;
        """
    )
    conn.commit()
    conn.close()


# ── RetrievedChunk ────────────────────────────────────────────────────────────

class TestRetrievedChunk:
    def test_basic_fields(self):
        chunk = RetrievedChunk(capture_id=1, text="工作记录", score=0.9, source="fts5")
        assert chunk.capture_id == 1
        assert chunk.text == "工作记录"
        assert chunk.score == pytest.approx(0.9)
        assert chunk.source == "fts5"

    def test_default_metadata(self):
        chunk = RetrievedChunk(capture_id=1, text="text")
        assert chunk.metadata == {"doc_key": "capture:1"}

    def test_default_score(self):
        chunk = RetrievedChunk(capture_id=1, text="text")
        assert chunk.score == 0.0

    def test_default_source(self):
        chunk = RetrievedChunk(capture_id=1, text="text")
        assert chunk.source == "unknown"

    def test_default_doc_key_uses_capture_id(self):
        chunk = RetrievedChunk(capture_id=7, text="text")
        assert chunk.doc_key == "capture:7"
        assert chunk.metadata["doc_key"] == "capture:7"


# ── RRF ──────────────────────────────────────────────────────────────────────

class TestRrf:
    def test_merges_two_lists(self):
        list1  = [_chunk(1, 0.9), _chunk(2, 0.8)]
        list2  = [_chunk(1, 0.7), _chunk(3, 0.6)]
        merged = reciprocal_rank_fusion([list1, list2], top_k=3)
        # chunk 1 出现在两个列表，RRF 分数最高
        assert merged[0].capture_id == 1

    def test_all_sources_marked_merged(self):
        list1  = [_chunk(1), _chunk(2)]
        merged = reciprocal_rank_fusion([list1])
        assert all(c.source == "merged" for c in merged)

    def test_empty_lists(self):
        assert reciprocal_rank_fusion([[], []]) == []

    def test_single_list(self):
        list1  = [_chunk(1), _chunk(2), _chunk(3)]
        merged = reciprocal_rank_fusion([list1], top_k=2)
        assert len(merged) == 2

    def test_top_k_respected(self):
        list1  = [_chunk(i) for i in range(10)]
        merged = reciprocal_rank_fusion([list1], top_k=4)
        assert len(merged) == 4

    def test_scores_positive(self):
        list1  = [_chunk(1), _chunk(2)]
        merged = reciprocal_rank_fusion([list1])
        assert all(c.score > 0 for c in merged)

    def test_deduplication(self):
        """同一 doc_key 出现在两个列表时，合并后只有一条"""
        list1  = [_chunk(1, doc_key="capture:1")]
        list2  = [_chunk(1, doc_key="capture:1")]
        merged = reciprocal_rank_fusion([list1, list2])
        doc_keys = [c.doc_key for c in merged]
        assert len(doc_keys) == len(set(doc_keys))

    def test_keeps_capture_and_knowledge_with_same_capture_id(self):
        capture_chunk = _chunk(1, source="fts5", doc_key="capture:1", metadata={"source_type": "capture", "doc_key": "capture:1"})
        knowledge_chunk = _chunk(1, source="knowledge", doc_key="knowledge:9", metadata={"source_type": "knowledge", "doc_key": "knowledge:9", "knowledge_id": 9})
        merged = reciprocal_rank_fusion([[capture_chunk], [knowledge_chunk]], top_k=2)
        assert {chunk.doc_key for chunk in merged} == {"capture:1", "knowledge:9"}

    def test_ranking_by_appearance(self):
        """出现在更多列表的 chunk 排名应更高"""
        list1 = [_chunk(10), _chunk(20)]
        list2 = [_chunk(20), _chunk(30)]
        list3 = [_chunk(20), _chunk(10)]
        merged = reciprocal_rank_fusion([list1, list2, list3])
        # chunk 20 出现在 3 个列表，应为第一名
        assert merged[0].capture_id == 20

    def test_custom_k(self):
        """不同 k 值应影响分数但结果数量不变"""
        list1    = [_chunk(1), _chunk(2)]
        merged60 = reciprocal_rank_fusion([list1], k=60)
        merged10 = reciprocal_rank_fusion([list1], k=10)
        assert len(merged60) == len(merged10) == 2
        # k=10 时分母更小，分数更高
        assert merged10[0].score > merged60[0].score

    def test_weighted_lists_prioritize_vector_source(self):
        keyword_only = _chunk(1, source="document", doc_key="document:1", metadata={"source_type": "document", "doc_key": "document:1"})
        vector_only = _chunk(2, source="vector", doc_key="document:2", metadata={"source_type": "document", "doc_key": "document:2"})
        merged = reciprocal_rank_fusion(
            [([keyword_only], 0.45), ([vector_only], 1.0)],
            top_k=2,
        )
        assert [chunk.doc_key for chunk in merged] == ["document:2", "document:1"]

    def test_min_score_filters_weak_keyword_only_matches(self):
        keyword_only = _chunk(1, source="document", doc_key="document:1", metadata={"source_type": "document", "doc_key": "document:1"})
        vector_only = _chunk(2, source="vector", doc_key="document:2", metadata={"source_type": "document", "doc_key": "document:2"})
        merged = reciprocal_rank_fusion(
            [([keyword_only], 0.45), ([vector_only], 1.0)],
            top_k=2,
            min_score=0.01,
        )
        assert [chunk.doc_key for chunk in merged] == ["document:2"]

    def test_baked_artifact_wins_doc_key_over_high_score_capture(self):
        """同 doc_key 冲突时烘焙产物内容优先于原始 capture（不受 BM25 原始分影响）"""
        artifact = _chunk(
            1, score=90.8, source="keyword", doc_key="document_url:https://x/a",
            metadata={"source_type": "document", "doc_key": "document_url:https://x/a"},
        )
        capture = _chunk(
            2, score=1009.0, source="capture_fts", doc_key="document_url:https://x/a",
            metadata={"source_type": "pending_document", "doc_key": "document_url:https://x/a"},
        )
        merged = reciprocal_rank_fusion([([capture], 0.7), ([artifact], 0.45)], top_k=2)
        assert len(merged) == 1
        assert merged[0].capture_id == 1
        assert (merged[0].metadata or {}).get("source_type") == "document"
        assert merged[0].metadata.get("retrieval_score") == 90.8

    def test_document_id_deduplicates_vector_and_url_artifact_keys(self):
        vector = _chunk(
            0, score=0.7, source="vector", doc_key="bake_document:366",
            metadata={
                "source_type": "document", "doc_key": "bake_document:366",
                "document_id": 366,
            },
        )
        artifact = _chunk(
            0, score=90.0, source="document",
            doc_key="document_url:https://docs.example/item",
            metadata={
                "source_type": "document",
                "doc_key": "document_url:https://docs.example/item",
                "artifact_id": 366,
            },
        )

        merged = reciprocal_rank_fusion([[vector], [artifact]], top_k=5)

        assert [chunk.doc_key for chunk in merged] == ["document:366"]


# ── 连续短语命中加成 ──────────────────────────────────────────────────────────

class TestPhraseBoost:
    def test_consecutive_query_phrases_extracts_cjk_runs_and_ascii(self):
        phrases = _consecutive_query_phrases("本周 AIGC 项目周报内容")
        assert "项目周报内容" in phrases
        assert "aigc" in phrases
        # 两字中文段不作为短语（由逐词计分覆盖）
        assert "本周" not in phrases

    def test_phrase_present_requires_consecutive_match(self):
        assert _phrase_present("团队项目周报汇总", "项目周报")
        assert not _phrase_present("项目进度与周报", "项目周报")

    def test_phrase_present_long_phrase_accepts_head_tail_coverage(self):
        # 长短语拆首尾 4 字子段：项目周报 + 周报内容 均连续出现即命中
        assert _phrase_present("共建项目周报的周报内容整理", "项目周报内容")
        assert not _phrase_present("只有项目周报", "项目周报内容")

    def test_phrase_boost_multiplies_field_hits(self):
        phrases = ["项目周报"]
        no_hit = _phrase_boost(phrases, "无关标题", "无关摘要", "无关正文")
        title_hit = _phrase_boost(phrases, "项目周报标题", "无关摘要", "无关正文")
        all_hit = _phrase_boost(phrases, "项目周报标题", "项目周报摘要", "项目周报正文")
        assert no_hit == 1.0
        assert title_hit > no_hit
        assert all_hit > title_hit

    def test_artifact_with_consecutive_phrase_outranks_scattered_match(self, tmp_path):
        """标题/摘要/正文连续命中查询短语的产物分数高于仅零散含词的产物"""
        db_path = str(tmp_path / "phrase-boost.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_knowledge (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT,
                detailed_content TEXT,
                entities TEXT,
                timeline_id INTEGER,
                source_timeline_ids TEXT DEFAULT '[]',
                source_capture_ids TEXT NOT NULL DEFAULT '[]',
                importance INTEGER DEFAULT 3,
                user_verified BOOLEAN DEFAULT 0,
                updated_at_ms INTEGER
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO bake_knowledge
                (id, title, summary, content, detailed_content, entities, updated_at_ms)
            VALUES (?, ?, ?, ?, '', '[]', 1720000000000)
            """,
            [
                (1, "GPU 成本优化项目周报", "项目周报的完整内容记录", "周报正文"),
                (2, "记录一次周报相关的排查", "内容零散", "提及周报与内容与项目"),
            ],
        )
        conn.commit()
        conn.row_factory = sqlite3.Row

        retriever = KnowledgeFts5Retriever(db_path)
        results = retriever._search_artifacts(
            conn.cursor(), "项目周报内容", top_k=5, entity_terms=None,
        )
        conn.close()
        scores = {chunk.doc_key: float(chunk.score or 0) for chunk in results}
        assert scores["bake_knowledge:1"] > scores["bake_knowledge:2"]


# ── RagPipeline ───────────────────────────────────────────────────────────────

class TestRagPipeline:
    def test_unified_selection_allows_all_supported_memory_sources(self):
        chunks = [
            _chunk(1, source="knowledge", doc_key="knowledge:1", metadata={"source_type": "knowledge", "doc_key": "knowledge:1", "knowledge_id": 1}),
            _chunk(2, source="document", doc_key="document:2", metadata={"source_type": "document", "doc_key": "document:2"}),
            _chunk(3, source="bake_knowledge", doc_key="bake_knowledge:3", metadata={"source_type": "bake_knowledge", "doc_key": "bake_knowledge:3"}),
            _chunk(4, source="operation", doc_key="operation:4", metadata={"source_type": "operation", "doc_key": "operation:4"}),
            _chunk(5, source="data", doc_key="data:5", metadata={"source_type": "data", "doc_key": "data:5"}),
            _chunk(6, source="pending_document", doc_key="pending_document:6", metadata={"source_type": "pending_document", "doc_key": "pending_document:6"}),
        ]

        selected = RagPipeline._select_contexts(chunks, top_k=10)

        assert [chunk.metadata["source_type"] for chunk in selected] == [
            "knowledge", "document", "bake_knowledge", "operation", "data", "pending_document"
        ]

    def test_raw_document_keyword_after_ax_prefix_is_recalled(self):
        deep_keyword = "潮汐特性"
        raw_text = "AX：" + ("背景资料 " * 100) + deep_keyword + "可用于弹性调度。"
        assert raw_text.index(deep_keyword) > 500
        raw_document = RetrievedChunk(
            capture_id=77,
            text=raw_text,
            score=1.0,
            source="fts5",
            doc_key="capture:77",
            metadata={
                "source_type": "capture",
                "doc_key": "capture:77",
                "url": "https://docs.example.com/k/home/example?from=search",
                "webpage_title": "潮汐调度说明",
            },
        )
        pipeline = _make_pipeline(fts_chunks=[raw_document], top_k=3)

        result = pipeline.query("潮汐特性", references_only=True)

        assert len(result.contexts) == 1
        assert result.contexts[0].metadata["source_type"] == "pending_document"
        assert result.contexts[0].doc_key == "document_url:https://docs.example.com/k/home/example"
        assert deep_keyword in result.contexts[0].text[:800]

    def test_linked_knowledge_document_is_promoted_before_context_selection(self):
        knowledge_hit = RetrievedChunk(
            capture_id=18_907,
            text="稳柱系统版本演进",
            score=0.61,
            source="knowledge",
            doc_key="knowledge:1884",
            metadata={
                "source_type": "knowledge",
                "knowledge_id": 1884,
                "doc_key": "knowledge:1884",
            },
        )
        promoted_document = RetrievedChunk(
            capture_id=0,
            text="文档：[更新日志] Wenz - 广告消耗异动归因系统",
            score=50.0,
            source="document",
            doc_key="document:87",
            metadata={
                "source_type": "document",
                "document_id": 87,
                "artifact_id": 87,
                "doc_key": "document:87",
            },
        )

        class PromotingKnowledgeRetriever(MockFts5Retriever):
            def __init__(self):
                super().__init__([knowledge_hit])
                self.promote_calls = 0

            def promote_documents_linked_to_knowledge(self, chunks, query, top_k, entity_terms):
                self.promote_calls += 1
                assert any(chunk.doc_key == "knowledge:1884" for chunk in chunks)
                return [promoted_document]

        knowledge_retriever = PromotingKnowledgeRetriever()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),  # type: ignore[arg-type]
            knowledge_retriever=knowledge_retriever,  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            top_k=3,
        )

        result = pipeline.query("稳柱软件版本更新记录", references_only=True)

        assert knowledge_retriever.promote_calls == 1
        assert result.contexts[0].metadata["document_id"] == 87

    def test_query_returns_rag_result(self):
        pipeline = _make_pipeline(
            fts_chunks=[_chunk(1, 0.8)],
        )
        result = pipeline.query("飞书会议")
        assert isinstance(result, RagResult)

    def test_answer_from_llm(self):
        pipeline = _make_pipeline(llm_response="记忆面包回答")
        result   = pipeline.query("任何问题")
        assert result.answer == "记忆面包回答"

    def test_stream_callbacks_deliver_contexts_before_answer_delta(self):
        pipeline = _make_pipeline(
            knowledge_chunks=[
                _chunk(
                    1,
                    0.9,
                    source="knowledge",
                    doc_key="knowledge:1",
                    metadata={
                        "source_type": "knowledge",
                        "doc_key": "knowledge:1",
                        "knowledge_id": 1,
                    },
                )
            ],
            llm_response="流式回答",
        )
        events: list[tuple[str, object]] = []

        result = pipeline.query(
            "工作内容",
            on_contexts=lambda contexts: events.append(("contexts", len(contexts))),
            on_delta=lambda text: events.append(("delta", text)),
        )

        assert events[0][0] == "contexts"
        assert events[1] == ("delta", "流式回答")
        assert result.answer == "流式回答"

    def test_contexts_included(self):
        knowledge = [
            _chunk(1, 0.9, source="bake_knowledge", doc_key="bake_knowledge:1", metadata={"source_type": "bake_knowledge", "doc_key": "bake_knowledge:1", "artifact_id": 1}),
            _chunk(2, 0.7, source="operation", doc_key="operation:2", metadata={"source_type": "operation", "doc_key": "operation:2", "artifact_id": 2}),
        ]
        pipeline = _make_pipeline(knowledge_chunks=knowledge)
        result = pipeline.query("工作内容")
        assert len(result.contexts) >= 1

    def test_model_in_result(self):
        pipeline = _make_pipeline()
        result   = pipeline.query("问题")
        assert result.model == "mock-llm"

    def test_parse_query_intent_resolves_this_week_period(self):
        """本周等相对时间应解析出确定性周期描述（含周次与起止日期）"""
        intent = RagPipeline._parse_query_intent("请提供 AIGC 项目的本周工作进展报告")
        assert intent.period_kind == "current_week"
        assert intent.period_phrase == "本周"
        assert "年第" in intent.period_display and "周" in intent.period_display
        assert "至" in intent.period_display
        assert intent.start_ts is not None

    def test_relative_time_clause_injected_into_prompt(self):
        """含本周的查询应在 prompt 中注入确定性时间口径，供模型佐证证据归属"""
        pipeline = _make_pipeline(
            knowledge_chunks=[
                _chunk(
                    1,
                    0.9,
                    source="knowledge",
                    doc_key="knowledge:1",
                    metadata={
                        "source_type": "knowledge",
                        "doc_key": "knowledge:1",
                        "knowledge_id": 1,
                        "observed_at": int(time.time() * 1000),
                    },
                )
            ],
        )
        pipeline.query("请总结我本周的工作进展")
        prompt = pipeline._llm.last_prompt  # type: ignore[attr-defined]
        assert "【时间口径】" in prompt
        assert "本周" in prompt
        assert "今天是" in prompt

    def test_no_time_clause_without_relative_time(self):
        pipeline = _make_pipeline()
        pipeline.query("X40 的价格是多少")
        prompt = pipeline._llm.last_prompt  # type: ignore[attr-defined]
        assert "【时间口径】" not in prompt

    def test_build_context_marks_artifact_updated_time(self):
        """烘焙产物缺少看到/事件时间时，应补充创建/更新时间供周期归属佐证"""
        ts = int(time.time() * 1000)
        artifact = _chunk(
            1,
            24.0,
            source="bake_knowledge",
            doc_key="bake_knowledge:9",
            metadata={"source_type": "bake_knowledge", "doc_key": "bake_knowledge:9", "updated_at": ts},
        )
        pending = _chunk(
            2,
            5.0,
            source="capture_fts",
            doc_key="pending_document:doc",
            metadata={"source_type": "pending_document", "doc_key": "pending_document:doc", "time": ts},
        )
        context = RagPipeline._build_context([artifact, pending])
        assert "创建/更新时间=" in context
        assert "采集时间=" in context

    def test_empty_context_still_answers(self):
        """无上下文时 LLM 仍然被调用"""
        pipeline = _make_pipeline(knowledge_chunks=[], vector_chunks=[])
        result = pipeline.query("没有上下文的问题")
        assert result.answer is not None

    def test_llm_called_once(self):
        llm      = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model  = EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever = MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever   = MockFts5Retriever(),    # type: ignore[arg-type]
            llm              = llm,
        )
        pipeline.query("问题")
        assert llm.call_count == 1

    def test_floating_assist_answer_uses_room_for_complete_output(self):
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model  = EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever = MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever   = MockFts5Retriever(),    # type: ignore[arg-type]
            knowledge_retriever = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")]),  # type: ignore[arg-type]
            llm              = llm,
        )

        pipeline.query(
            "核心问题：大模型成本效率怎么拉齐\n"
            "检索问题：大模型 成本 效率 GPU 资源\n"
            "输出格式：\n"
            "## 用户问题理解\n"
            "用一句话说明用户当前真正想问什么。\n"
            "## 回答\n"
            "给结论和依据。"
        )

        assert llm.last_kwargs["num_predict"] == 8192

    def test_length_done_reason_marks_output_truncated(self):
        llm = MockLlmBackend(done_reason="length")
        pipeline = RagPipeline(
            embedding_model  = EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever = MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever   = MockFts5Retriever(),    # type: ignore[arg-type]
            knowledge_retriever = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")]),  # type: ignore[arg-type]
            llm              = llm,
        )

        result = pipeline.query("问题")

        assert result.done_reason == "length"
        assert result.output_truncated is True

    def test_top_k_limits_contexts(self):
        knowledge = [
            _chunk(i, 0.9 - i * 0.05, source="knowledge", doc_key=f"knowledge:{i}", metadata={"source_type": "knowledge", "doc_key": f"knowledge:{i}", "knowledge_id": i})
            for i in range(10)
        ]
        pipeline = _make_pipeline(knowledge_chunks=knowledge, top_k=3)
        result = pipeline.query("问题")
        assert len(result.contexts) <= 3

    def test_query_keeps_document_artifact_after_rrf_truncation(self):
        bake_chunks = [
            _chunk(
                i,
                0.9 - i * 0.01,
                source="bake_knowledge",
                doc_key=f"bake_knowledge:{i}",
                metadata={"source_type": "bake_knowledge", "doc_key": f"bake_knowledge:{i}", "artifact_id": i},
            )
            for i in range(1, 11)
        ]
        smact_doc = _chunk(
            0,
            120.0,
            source="document",
            doc_key="document:80",
            metadata={"source_type": "document", "doc_key": "document:80", "artifact_id": 80, "document_id": 80},
        )
        pipeline = _make_pipeline(
            knowledge_chunks=[*bake_chunks, smact_doc],
            vector_chunks=bake_chunks[:10],
            top_k=3,
        )

        result = pipeline.query("GPU 利用率 SMACT 指标")

        assert any(chunk.doc_key == "document:80" for chunk in result.contexts)

    def test_query_deduplicates_document_id_and_url_rescue_for_same_artifact(self):
        source_url = "https://docs.example/container-gpu"
        document_by_id = _chunk(
            0,
            120.0,
            source="document",
            doc_key="document:80",
            metadata={
                "source_type": "document",
                "doc_key": "document:80",
                "artifact_id": 80,
                "document_id": 80,
                "source_url": source_url,
                "title": "容器云 GPU 指标采集项目",
            },
        )
        same_document_by_url = _chunk(
            0,
            110.0,
            source="document",
            doc_key=f"document_url:{source_url}",
            metadata={
                "source_type": "document",
                "doc_key": f"document_url:{source_url}",
                "artifact_id": 80,
                "document_id": 80,
                "source_url": source_url,
                "title": "容器云 GPU 指标采集项目",
            },
        )
        pipeline = _make_pipeline(
            knowledge_chunks=[document_by_id, same_document_by_url],
            vector_chunks=[document_by_id],
            top_k=3,
        )

        result = pipeline.query("SMACT 产品简介")

        matches = [
            chunk
            for chunk in result.contexts
            if (chunk.metadata or {}).get("document_id") == 80
        ]
        assert len(matches) == 1

    def test_unified_selection_keeps_rrf_order_across_incomparable_source_scores(self):
        keyword_rescue = RetrievedChunk(
            capture_id=0,
            text="关键词产物",
            score=164.0,
            source="bake_knowledge",
            doc_key="bake_knowledge:1",
            metadata={
                "source_type": "bake_knowledge",
                "doc_key": "bake_knowledge:1",
                "artifact_id": 1,
                "selection_origin": "artifact_rescue",
            },
        )
        fused = [
            RetrievedChunk(
                capture_id=0,
                text=f"融合候选 {document_id}",
                score=rrf_score,
                source="merged",
                doc_key=f"document:{document_id}",
                metadata={
                    "source_type": "document",
                    "doc_key": f"document:{document_id}",
                    "document_id": document_id,
                    "retrieval_score": retrieval_score,
                    "rrf_score": rrf_score,
                },
            )
            for document_id, retrieval_score, rrf_score in (
                (366, 0.67, 0.0163),
                (80, 0.65, 0.0158),
                (173, 0.64, 0.0149),
            )
        ]

        selected = RagPipeline._select_contexts(
            [*fused, keyword_rescue],
            top_k=3,
        )

        assert [chunk.doc_key for chunk in selected] == [
            "document:366",
            "document:80",
            "bake_knowledge:1",
        ]

    def test_unified_selection_limits_artifact_rescue_to_one_slot(self):
        fused = [
            _chunk(
                index,
                score=0.02 - index * 0.001,
                source="merged",
                doc_key=f"document:{index}",
                metadata={
                    "source_type": "document",
                    "doc_key": f"document:{index}",
                    "document_id": index,
                    "rrf_score": 0.02 - index * 0.001,
                },
            )
            for index in range(1, 6)
        ]
        rescues = [
            _chunk(
                100 + index,
                score=100.0 - index,
                source="bake_knowledge",
                doc_key=f"bake_knowledge:{100 + index}",
                metadata={
                    "source_type": "bake_knowledge",
                    "doc_key": f"bake_knowledge:{100 + index}",
                    "artifact_id": 100 + index,
                    "selection_origin": "artifact_rescue",
                },
            )
            for index in range(4)
        ]

        selected = RagPipeline._select_contexts(
            [*fused, *rescues],
            top_k=5,
        )

        assert sum(
            1
            for chunk in selected
            if (chunk.metadata or {}).get("selection_origin") == "artifact_rescue"
        ) == 1
        assert [chunk.doc_key for chunk in selected[:4]] == [
            "document:1",
            "document:2",
            "document:3",
            "document:4",
        ]

    def test_unified_selection_gives_larger_top_k_more_rescue_slots(self):
        fused = [
            _chunk(
                index,
                score=0.02 - index * 0.001,
                source="merged",
                doc_key=f"document:{index}",
                metadata={
                    "source_type": "document",
                    "doc_key": f"document:{index}",
                    "document_id": index,
                    "rrf_score": 0.02 - index * 0.001,
                },
            )
            for index in range(1, 12)
        ]
        rescues = [
            _chunk(
                100 + index,
                score=100.0 - index,
                source="document",
                doc_key=f"document_url:https://docs.example/{100 + index}",
                metadata={
                    "source_type": "document",
                    "doc_key": f"document_url:https://docs.example/{100 + index}",
                    "document_id": 100 + index,
                    "selection_origin": "artifact_rescue",
                },
            )
            for index in range(4)
        ]

        selected = RagPipeline._select_contexts(
            [*fused, *rescues],
            top_k=10,
        )

        rescue_keys = [
            chunk.doc_key
            for chunk in selected
            if (chunk.metadata or {}).get("selection_origin") == "artifact_rescue"
        ]
        # top_k=10 时应保留 2 个补位槽，次高词法命中不再被单个高分候选压制。
        assert rescue_keys == [
            "document_url:https://docs.example/100",
            "document_url:https://docs.example/101",
        ]
        assert len(selected) == 10

    def test_query_intent_passes_time_and_entity_filters(self):
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        vector_r = MockVectorRetriever()
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我最近使用 gemini 做了什么")
        assert knowledge.last_kwargs["start_ts"] is not None
        assert "gemini" in (knowledge.last_kwargs["entity_terms"] or [])
        filters = vector_r.last_kwargs["filters"]
        assert filters is not None
        assert filters.start_ts is not None
        assert filters.source_types == ["knowledge", "document"]
        # 近期活动总结不以应用名再次收窄向量召回。
        assert filters.app_names is None

    def test_chinese_question_extracts_meaningful_terms(self):
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        vector_r = MockVectorRetriever()
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("昨天那段知识总结里提到的数据库迁移是什么")
        entity_terms = knowledge.last_kwargs["entity_terms"] or []
        assert "数据库" in entity_terms
        assert "迁移" in entity_terms
        assert "昨天那段知识总结里提到的数据库迁移是什么" not in entity_terms

    def test_query_intent_applies_ask_ai_policy(self):
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        vector_r = MockVectorRetriever()
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我今天问 Gemini 了什么")
        assert knowledge.last_kwargs["observed_start_ts"] is not None
        # summary/ask_ai 标记只影响排序与 top_k，不再向检索层传收窄过滤
        assert knowledge.last_kwargs["activity_types"] is None
        assert knowledge.last_kwargs["history_view"] is None
        assert knowledge.last_kwargs["is_self_generated"] is False
        assert knowledge.last_kwargs["evidence_strengths"] is None
        filters = vector_r.last_kwargs["filters"]
        assert filters.observed_start_ts is not None
        assert filters.activity_types is None
        assert filters.history_view is None
        assert filters.is_self_generated is False

    def test_query_intent_applies_history_policy(self):
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        vector_r = MockVectorRetriever()
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我今天看了什么历史消息")
        assert knowledge.last_kwargs["observed_start_ts"] is not None
        # 历史类查询保留时间语义，但不再向检索层传收窄过滤
        assert knowledge.last_kwargs["history_view"] is None
        assert knowledge.last_kwargs["content_origins"] is None
        assert knowledge.last_kwargs["activity_types"] is None
        filters = vector_r.last_kwargs["filters"]
        assert filters.history_view is None
        assert filters.content_origins is None

    def test_recent_query_uses_time_filter_without_switching_retrieval_mode(self):
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        vector_r = MockVectorRetriever()
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我最近关于aigc的工作有哪些")
        assert "query_mode" not in knowledge.last_kwargs
        assert knowledge.last_kwargs["observed_start_ts"] is not None
        assert "aigc" in (knowledge.last_kwargs["entity_terms"] or [])
        filters = vector_r.last_kwargs["filters"]
        assert filters.app_names in (None, [])

    def test_report_keyword_query_not_gated(self):
        """含"周报"的查询不再触发任务门控：knowledge 收到非空查询词，pending_document 通道被调用"""
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        fts5 = MockFts5Retriever()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=fts5,                     # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=MockLlmBackend(),
        )
        pipeline.query("AIGC 项目周报总结")
        assert knowledge.last_kwargs["query"] == "AIGC 项目周报总结"
        assert fts5.call_count >= 1
        assert fts5.last_kwargs["query"] == "AIGC 项目周报总结"

    def test_pending_capture_skipped_when_url_already_baked(self, tmp_path):
        """已有烘焙产物的 URL 不走 capture 兜底，烘焙正文进入上下文"""
        import sqlite3
        url = "https://docs.corp.example.com/k/home/abc/def"
        db_path = str(tmp_path / "memory.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE bake_documents (id INTEGER PRIMARY KEY, source_url TEXT, deleted_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO bake_documents (id, source_url, deleted_at) VALUES (582, ?, NULL)",
            (url,),
        )
        conn.commit()
        conn.close()

        doc_key = "document_url:" + url
        artifact = RetrievedChunk(
            capture_id=0,
            text="烘焙周报正文内容",
            score=90.8,
            source="knowledge",
            doc_key=doc_key,
            metadata={"source_type": "document", "doc_key": doc_key, "artifact_id": 582},
        )
        capture = RetrievedChunk(
            capture_id=46384,
            text="AIGC 共建项目周报 " + "目录导航无权限文本 " * 30,
            score=1009.8,
            source="fts5",
            doc_key="capture:46384",
            metadata={"url": url, "win_title": "2026Q2 AIGC 共建项目周报"},
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),      # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(chunks=[capture]),   # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[artifact]),  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            db_path=db_path,
        )
        result = pipeline.query("AIGC 项目周报总结", references_only=True)
        selected = [
            c for c in result.contexts
            if (c.metadata or {}).get("artifact_id") == 582
        ]
        assert len(selected) == 1
        assert selected[0].doc_key == "document:582"
        assert "烘焙周报正文内容" in selected[0].text
        assert (selected[0].metadata or {}).get("source_type") == "document"

    def test_unified_selection_selects_baked_document_chunks(self):
        """activity_type='document' 的烘焙文档可沿统一链路入选。"""
        doc_chunk = RetrievedChunk(
            capture_id=50,
            text="AIGC 项目周报文档内容",
            score=0.6,
            source="knowledge",
            doc_key="document:50",
            metadata={
                "source_type": "document",
                "doc_key": "document:50",
                "activity_type": "document",
                "importance": 3,
            },
        )
        selected = RagPipeline._select_contexts([doc_chunk], top_k=5)
        assert any(chunk.doc_key == "document:50" for chunk in selected)

    def test_unified_selection_does_not_replace_rrf_order_with_importance(self):
        """importance 不能通过离散模式整体覆盖已经融合好的 RRF 顺序。"""
        low = RetrievedChunk(
            capture_id=1,
            text="低重要性浏览记录",
            score=0.9,
            source="knowledge",
            doc_key="knowledge:1",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:1",
                "importance": 1,
                "activity_type": "reading",
            },
        )
        high = RetrievedChunk(
            capture_id=2,
            text="高重要性开发记录",
            score=0.5,
            source="knowledge",
            doc_key="knowledge:2",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:2",
                "importance": 5,
                "activity_type": "coding",
            },
        )
        selected = RagPipeline._select_contexts([low, high], top_k=5)
        assert [chunk.doc_key for chunk in selected] == ["knowledge:1", "knowledge:2"]

    def test_time_window_empty_expands_to_recent_14_days(self):
        """显式时间窗召回为空时，自动扩大到最近 14 天重试一次"""
        fallback_chunk = _chunk(9, source="knowledge")
        calls: list[dict] = []

        class EmptyThenFallbackKnowledge:
            def search(self, query, top_k=10, **kwargs):
                calls.append({"query": query, "top_k": top_k, **kwargs})
                if len(calls) >= 2:
                    return [fallback_chunk]
                return []

        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=EmptyThenFallbackKnowledge(),  # type: ignore[arg-type]
            llm=MockLlmBackend(),
        )
        result = pipeline.query("生成下本周的工作周报", references_only=True)

        assert len(calls) == 2
        assert calls[1]["observed_start_ts"] is not None
        now_ms = int(time.time() * 1000)
        fourteen_days_ms = 14 * 24 * 60 * 60 * 1000
        assert calls[1]["observed_start_ts"] >= now_ms - fourteen_days_ms - 5000
        assert calls[1]["observed_start_ts"] <= now_ms
        assert any(chunk.doc_key == "capture:9" for chunk in result.contexts)

    def test_select_contexts_filters_noise_knowledge(self):
        llm = MockLlmBackend()
        noise_chunk = RetrievedChunk(
            capture_id=1,
            text="概述：低价值工作片段（invalid_json）",
            score=0.95,
            source="knowledge",
            doc_key="knowledge:1",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:1",
                "knowledge_id": 1,
                "overview": "低价值工作片段（invalid_json）",
                "activity_type": "other",
                "content_origin": "other",
                "evidence_strength": "low",
            },
        )
        good_chunk = RetrievedChunk(
            capture_id=2,
            text="概述：本周推进 AIGC 页面方案",
            score=0.8,
            source="knowledge",
            doc_key="knowledge:2",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:2",
                "knowledge_id": 2,
                "overview": "本周推进 AIGC 页面方案",
                "activity_type": "coding",
                "content_origin": "live_interaction",
                "evidence_strength": "high",
            },
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(chunks=[good_chunk]),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[noise_chunk]),  # type: ignore[arg-type]
            llm=llm,
        )
        result = pipeline.query("我最近关于aigc的工作有哪些")
        assert [chunk.doc_key for chunk in result.contexts] == ["knowledge:2"]

        llm = MockLlmBackend()
        chunk = RetrievedChunk(
            capture_id=1,
            text="概述：今天回看昨天的飞书消息",
            score=0.8,
            source="knowledge",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:1",
                "knowledge_id": 1,
                "observed_at": 1_710_000_100_000,
                "event_time_start": 1_709_913_600_000,
                "event_time_end": 1_709_914_000_000,
                "history_view": True,
                "activity_type": "reviewing_history",
                "content_origin": "historical_content",
            },
            doc_key="knowledge:1",
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[chunk]),  # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我今天看了什么历史消息")
        assert "看到时间=" in llm.last_prompt
        assert "事件时间=" in llm.last_prompt
        assert "历史回看" in llm.last_prompt
        assert "来源=historical_content" in llm.last_prompt

        llm = MockLlmBackend()
        chrome_chunk = RetrievedChunk(
            capture_id=1,
            text="应用：Google Chrome\n窗口：Claude",
            score=0.8,
            source="knowledge",
            metadata={"source_type": "knowledge", "app_name": "Google Chrome", "doc_key": "knowledge:1", "knowledge_id": 1},
            doc_key="knowledge:1",
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[chrome_chunk]),  # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("我最近用Google Chrome了吗")
        assert "Google Chrome" in llm.last_prompt

    def test_knowledge_context_prioritized_in_prompt(self):
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(chunks=[
                _chunk(2, source="vector", doc_key="capture:2", metadata={"source_type": "capture", "doc_key": "capture:2"})
            ]),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(chunks=[
                _chunk(1, source="fts5", doc_key="capture:1", metadata={"source_type": "capture", "doc_key": "capture:1"})
            ]),        # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[
                _chunk(3, score=10.0, source="bake_knowledge", doc_key="bake_knowledge:3", metadata={"source_type": "bake_knowledge", "doc_key": "bake_knowledge:3", "artifact_id": 3})
            ]),  # type: ignore[arg-type]
            llm=llm,
            top_k=3,
        )
        pipeline.query("Gemini")
        first_context_line = llm.last_prompt.splitlines()[1]
        assert "[bake_knowledge]" in first_context_line

    def test_query_only_keeps_knowledge_contexts(self):
        llm = MockLlmBackend()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(chunks=[
                _chunk(2, source="vector", doc_key="capture:2", metadata={"source_type": "capture", "doc_key": "capture:2"}),
                _chunk(3, source="vector", doc_key="knowledge:3", metadata={"source_type": "knowledge", "doc_key": "knowledge:3", "knowledge_id": 3}),
            ]),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(chunks=[
                _chunk(1, source="fts5", doc_key="capture:1", metadata={"source_type": "capture", "doc_key": "capture:1"}),
            ]),  # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[
                _chunk(10, source="knowledge", doc_key="knowledge:10", metadata={"source_type": "knowledge", "doc_key": "knowledge:10", "knowledge_id": 10}),
            ]),  # type: ignore[arg-type]
            llm=llm,
            top_k=3,
        )
        result = pipeline.query("最近做了什么")
        assert all(chunk.metadata.get("source_type") == "knowledge" for chunk in result.contexts)
        assert {chunk.doc_key for chunk in result.contexts} == {"knowledge:10", "knowledge:3"}

    def test_query_uses_runtime_top_k(self):
        knowledge = [
            _chunk(i, 0.9 - i * 0.05, source="knowledge", doc_key=f"knowledge:{i}", metadata={"source_type": "knowledge", "doc_key": f"knowledge:{i}", "knowledge_id": i})
            for i in range(10)
        ]
        pipeline = _make_pipeline(knowledge_chunks=knowledge, top_k=5)
        result = pipeline.query("问题", top_k=2)
        assert len(result.contexts) <= 2

    def test_wrapped_aigc_link_query_keeps_strict_top_k_and_core_query(self):
        knowledge = [
            _chunk(
                index,
                0.9 - index * 0.01,
                source="knowledge",
                doc_key=f"knowledge:{index}",
                metadata={
                    "source_type": "knowledge",
                    "doc_key": f"knowledge:{index}",
                    "knowledge_id": index,
                },
            )
            for index in range(30)
        ]
        retriever = MockFts5Retriever(chunks=knowledge)
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),  # type: ignore[arg-type]
            knowledge_retriever=retriever,  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            top_k=10,
        )
        wrapped_query = (
            "核心问题：如何获取 AIGC 共建的周报表？\n"
            "检索问题：AIGC 共建周报地址\n"
            "用户问题理解：请回答用户的问题。"
        )

        result = pipeline.query(wrapped_query, top_k=10, references_only=True)

        assert retriever.last_kwargs["query"] == "AIGC 共建周报地址"
        assert "query_mode" not in retriever.last_kwargs
        assert len(result.contexts) == 10

    def test_summary_word_does_not_expand_requested_top_k(self):
        knowledge = [
            _chunk(
                index,
                source="knowledge",
                doc_key=f"knowledge:{index}",
                metadata={
                    "source_type": "knowledge",
                    "doc_key": f"knowledge:{index}",
                    "knowledge_id": index,
                },
            )
            for index in range(20)
        ]
        pipeline = _make_pipeline(knowledge_chunks=knowledge, top_k=10)

        result = pipeline.query("总结本周 AIGC 共建进展", top_k=4, references_only=True)

        assert len(result.contexts) <= 4

    def test_embedding_failure_degrades_gracefully(self):
        """embedding 失败应降级为持久记忆检索，不抛异常"""
        knowledge = [
            _chunk(1, 0.8, source="bake_knowledge", doc_key="bake_knowledge:1", metadata={"source_type": "bake_knowledge", "doc_key": "bake_knowledge:1", "artifact_id": 1})
        ]
        pipeline = _make_pipeline(
            knowledge_chunks=knowledge,
            embed_raise=RuntimeError("embedding 服务不可用"),
        )
        result = pipeline.query("工作记录")
        assert result.answer is not None
        assert len(result.contexts) >= 1
        assert all(chunk.metadata.get("source_type") == "bake_knowledge" for chunk in result.contexts)

    def test_prompt_contains_query(self):
        llm = MockLlmBackend(model_name="qwen2.5:3b")
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(), # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("一个普通查询")
        assert "一个普通查询" in llm.last_prompt

    def test_vector_retriever_used_when_embedding_succeeds(self):
        vector_r = MockVectorRetriever(chunks=[
            _chunk(99, source="vector", doc_key="knowledge:99", metadata={"source_type": "knowledge", "doc_key": "knowledge:99", "knowledge_id": 99})
        ])
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(), # type: ignore[arg-type]
            llm=MockLlmBackend(),
        )
        pipeline.query("问题")
        assert vector_r.call_count == 1
        assert vector_r.last_kwargs["filters"].source_types == ["knowledge", "document"]
        assert vector_r.last_kwargs["score_threshold"] == pytest.approx(0.45)

    def test_lookup_prefers_vector_and_drops_weak_keyword_only_documents(self):
        relevant = _chunk(
            65,
            source="vector",
            doc_key="document:65",
            metadata={"source_type": "document", "doc_key": "document:65", "title": "万擎内部开源模型性能压测表"},
        )
        weak_keyword = _chunk(
            55,
            source="document",
            doc_key="document:55",
            metadata={"source_type": "document", "doc_key": "document:55", "title": "万擎 - 私有大模型 - 模型部署接口设计"},
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(chunks=[relevant]),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[weak_keyword]),  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            top_k=3,
        )
        result = pipeline.query("万擎的模型压测结果是什么样的？")
        assert [chunk.doc_key for chunk in result.contexts] == ["document:65"]

    def test_keyword_results_remain_fallback_when_vector_has_no_hits(self):
        keyword_only = _chunk(
            65,
            source="document",
            doc_key="document:65",
            metadata={"source_type": "document", "doc_key": "document:65", "title": "万擎内部开源模型性能压测表"},
        )
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(chunks=[]),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[keyword_only]),  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            top_k=3,
        )
        result = pipeline.query("万擎的模型压测结果是什么样的？")
        assert [chunk.doc_key for chunk in result.contexts] == ["document:65"]

    def test_vector_retriever_skipped_when_embedding_fails(self):
        """embedding 失败时，向量检索应跳过"""
        vector_r = MockVectorRetriever()
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(
                backend=MockEmbeddingBackend(should_raise=RuntimeError("embedding 失败"))
            ),
            vector_retriever=vector_r,               # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(), # type: ignore[arg-type]
            llm=MockLlmBackend(),
        )
        pipeline.query("问题")
        assert vector_r.call_count == 0


# ── VectorRetriever 接口测试 ──────────────────────────────────────────────────

class TestVectorRetriever:
    def test_is_available_returns_bool(self):
        retriever = VectorRetriever()
        assert isinstance(retriever.is_available(), bool)

    def test_search_returns_empty_when_unavailable(self):
        retriever = VectorRetriever()
        if not retriever.is_available():
            results = retriever.search([0.1, 0.2, 0.3], top_k=5)
            assert results == []

    def test_search_empty_vector_returns_empty(self):
        retriever = VectorRetriever()
        results   = retriever.search([], top_k=5)
        assert results == []

    def test_vector_retriever_filter_build_returns_none_for_empty_filter(self):
        assert VectorRetriever._build_qdrant_filter(VectorSearchFilter()) is None

    def test_lazy_init(self):
        retriever = VectorRetriever()
        assert retriever._client is None


class TestSqliteRetrievers:
    def test_knowledge_fallback_limits_terms_and_backfills_primary_capture_link(self, tmp_path):
        db_path = str(tmp_path / "knowledge-fallback.db")
        _init_knowledge_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE captures (id INTEGER PRIMARY KEY, url TEXT, webpage_title TEXT)"
        )
        conn.execute(
            "INSERT INTO captures (id, url, webpage_title) VALUES (?, ?, ?)",
            (100, "https://docs.example/fast-retrieval", "快速召回说明"),
        )
        conn.execute(
            """
            INSERT INTO timelines
                (id, capture_id, summary, overview, details, start_time, end_time,
                 duration_minutes, frag_app_name, frag_win_title, entities, category, user_verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                100,
                "召回优化",
                "知识库参考资料召回优化",
                "避免相关子查询扫描截图表",
                1_710_000_000_000,
                1_710_000_060_000,
                1,
                "MemoryBread",
                "咨询",
                "[]",
                "开发",
                1,
            ),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        retriever = KnowledgeFts5Retriever(db_path)
        results = retriever._search_by_app_fields(
            conn.cursor(),
            query="知识库参考资料召回优化",
            top_k=5,
            start_ts=None,
            end_ts=None,
            entity_terms=None,
            observed_start_ts=None,
            observed_end_ts=None,
            event_start_ts=None,
            event_end_ts=None,
            activity_types=None,
            content_origins=None,
            history_view=None,
            is_self_generated=None,
            evidence_strengths=None,
            created_start_ts=None,
            created_end_ts=None,
        )
        conn.close()

        fallback_sql = next(sql for sql in statements if "FROM timelines k" in sql)
        assert "group_concat" not in fallback_sql
        assert "capture_ids" not in fallback_sql
        assert len(_bounded_fallback_terms([f"term-{index}" for index in range(30)])) == 12
        assert results[0].metadata["url"] == "https://docs.example/fast-retrieval"
        assert results[0].metadata["webpage_title"] == "快速召回说明"

    def test_fts5_searches_full_ax_body_after_500_characters(self, tmp_path):
        db_path = str(tmp_path / "captures.db")
        _init_captures_db(db_path)
        ax_text = ("背景资料 " * 110) + "潮汐特性 可用于弹性调度"
        assert ax_text.index("潮汐特性") > 500

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO captures
                (id, ts, app_name, win_title, url, webpage_title, ocr_text, ax_text, input_text, audio_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1_710_000_000_000,
                "Chrome",
                "潮汐调度说明",
                "https://docs.example.com/k/home/example",
                "潮汐调度说明",
                "",
                ax_text,
                "",
                "",
            ),
        )
        conn.commit()
        conn.close()

        results = Fts5Retriever(db_path).search("潮汐特性", top_k=5)

        assert [chunk.capture_id for chunk in results] == [1]
        assert "潮汐特性" in results[0].text

    def test_capture_fallback_prioritizes_old_entity_before_recency_cutoff(self, tmp_path):
        db_path = str(tmp_path / "capture-entity-priority.db")
        _init_captures_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO captures
                (id, ts, app_name, win_title, url, webpage_title, ocr_text, ax_text, input_text, audio_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "Chrome",
                "Wenz 更新日志",
                "https://docs.example.com/d/home/wenz",
                "Wenz 更新日志",
                "",
                "稳柱是一款业务指标异动归因系统，包含 V2.0 到 V2.2 的版本演进。",
                "",
                "",
            ),
        )
        conn.executemany(
            """
            INSERT INTO captures
                (id, ts, app_name, win_title, url, webpage_title, ocr_text, ax_text, input_text, audio_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    capture_id,
                    capture_id,
                    "Chrome",
                    f"通用更新记录 {capture_id}",
                    f"https://docs.example/generic-{capture_id}",
                    f"通用更新记录 {capture_id}",
                    "",
                    "软件版本说明及更新记录，不包含目标产品实体。",
                    "",
                    "",
                )
                for capture_id in range(2, 302)
            ],
        )
        conn.commit()
        conn.close()

        results = Fts5Retriever(db_path).search("稳柱软件版本更新记录", top_k=5)

        assert 1 in [chunk.capture_id for chunk in results]

    def test_linked_knowledge_promotes_the_corresponding_document(self, tmp_path):
        db_path = str(tmp_path / "linked-document.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                linked_knowledge_ids TEXT NOT NULL DEFAULT '[]',
                deleted_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE bake_artifact_source_links (
                artifact_kind TEXT NOT NULL,
                artifact_id INTEGER NOT NULL,
                source_timeline_id INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO bake_documents
                (id, title, doc_type, summary, full_content, source_url,
                 source_memory_ids, linked_knowledge_ids, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                87,
                "[更新日志] Wenz - 广告消耗异动归因系统",
                "技术文档",
                "记录 Wenz 的版本演进。",
                "Wenz V2.0、V2.1、V2.2 版本说明。",
                "https://docs.example/wenz",
                '["605","1884"]',
                '["605","1884"]',
                1_720_000_000_000,
            ),
        )
        conn.execute(
            """
            INSERT INTO bake_artifact_source_links
                (artifact_kind, artifact_id, source_timeline_id)
            VALUES ('knowledge', 605, 1884)
            """
        )
        conn.commit()
        conn.close()

        knowledge_hit = RetrievedChunk(
            capture_id=18_907,
            text="稳柱系统的版本演进知识",
            score=0.61,
            source="vector",
            doc_key="knowledge:1884",
            metadata={
                "source_type": "knowledge",
                "knowledge_id": 1884,
                "doc_key": "knowledge:1884",
            },
        )
        promoted = KnowledgeFts5Retriever(db_path).promote_documents_linked_to_knowledge(
            [knowledge_hit],
            "稳柱软件版本更新记录",
            top_k=5,
        )

        assert [chunk.metadata["document_id"] for chunk in promoted] == [87]
        assert promoted[0].metadata["retrieval_method"] == "linked_knowledge"
        assert promoted[0].metadata["promoted_by_knowledge_ids"] == ["605"]

    def test_bake_knowledge_artifact_id_promotes_linked_document(self, tmp_path):
        db_path = str(tmp_path / "bake-linked-document.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                linked_knowledge_ids TEXT NOT NULL DEFAULT '[]',
                deleted_at INTEGER,
                updated_at INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO bake_documents
                (id, title, doc_type, summary, full_content, source_url,
                 source_memory_ids, linked_knowledge_ids, updated_at)
            VALUES (80, '容器云 GPU 指标采集项目', '技术文档',
                    'SMACT 指标说明', 'SMACT 用于衡量空分利用率。',
                    'https://docs.example/smact', '["1079"]', '["482"]', 1000)
            """
        )
        conn.commit()
        conn.close()

        knowledge_hit = RetrievedChunk(
            capture_id=0,
            text="SMACT 空分利用率",
            score=164.0,
            source="bake_knowledge",
            doc_key="bake_knowledge:482",
            metadata={
                "source_type": "bake_knowledge",
                "artifact_id": 482,
                "source_timeline_ids": ["1079"],
                "doc_key": "bake_knowledge:482",
            },
        )

        promoted = KnowledgeFts5Retriever(db_path).promote_documents_linked_to_knowledge(
            [knowledge_hit],
            "SMACT 产品介绍",
            top_k=5,
        )

        assert [chunk.metadata["document_id"] for chunk in promoted] == [80]
        assert promoted[0].metadata["promoted_by_knowledge_ids"] == ["482"]

    def test_legacy_timeline_id_collision_does_not_promote_without_source_link(self, tmp_path):
        db_path = str(tmp_path / "timeline-id-collision.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                linked_knowledge_ids TEXT NOT NULL DEFAULT '[]',
                deleted_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE bake_artifact_source_links (
                artifact_kind TEXT NOT NULL,
                artifact_id INTEGER NOT NULL,
                source_timeline_id INTEGER NOT NULL
            );
            INSERT INTO bake_documents
                (id, title, doc_type, linked_knowledge_ids, updated_at)
            VALUES (80, '无关文档', '技术文档', '["482"]', 1000);
            """
        )
        conn.commit()
        conn.close()

        legacy_hit = RetrievedChunk(
            capture_id=1,
            text="时间线知识",
            score=0.8,
            source="knowledge",
            doc_key="knowledge:482",
            metadata={
                "source_type": "knowledge",
                "knowledge_id": 482,
                "doc_key": "knowledge:482",
            },
        )

        promoted = KnowledgeFts5Retriever(db_path).promote_documents_linked_to_knowledge(
            [legacy_hit],
            "时间线知识",
            top_k=5,
        )

        assert promoted == []

    def test_artifact_search_honors_explicit_document_type(self, tmp_path):
        db_path = str(tmp_path / "artifacts.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                linked_knowledge_ids TEXT NOT NULL DEFAULT '[]',
                deleted_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE bake_knowledge (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT,
                detailed_content TEXT,
                entities TEXT,
                timeline_id INTEGER,
                source_timeline_ids TEXT DEFAULT '[]',
                source_capture_ids TEXT NOT NULL DEFAULT '[]',
                importance INTEGER DEFAULT 3,
                user_verified BOOLEAN DEFAULT 0,
                updated_at_ms INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO bake_documents
                (id, title, doc_type, summary, full_content, source_url, source_memory_ids, linked_knowledge_ids, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                80,
                "容器云 GPU 指标采集项目",
                "技术文档",
                "分析 GPUTL、SMACT、SMOCC 三种指标的物理意义与局限性。",
                "GPUTL 无法反映空间利用率，SMACT 用于衡量空分利用率，SMOCC 衡量饱和度。",
                "https://docs.example/container-gpu",
                '["562"]',
                '["562"]',
                1_720_000_000_000,
            ),
        )
        conn.execute(
            """
            INSERT INTO bake_knowledge
                (id, title, summary, content, detailed_content, entities, timeline_id, source_timeline_ids, source_capture_ids, importance, user_verified, updated_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                229,
                "该项目旨在解决现有 GPUTL 指标无法反映硅片内空间分布利用率的缺陷，引入 SMACT 和 SMOCC 指标构建多维度评估模型。",
                "该项目旨在解决现有 GPUTL 指标无法反映硅片内空间分布利用率的缺陷，引入 SMACT 和 SMOCC 指标构建多维度评估模型。",
                "GPUTL、SMACT、SMOCC 三类 GPU 利用率指标",
                "SMACT 表示空分利用率，SMOCC 表示饱和度。",
                "[]",
                562,
                "[]",
                "[]",
                3,
                0,
                1_720_000_000_000,
            ),
        )
        conn.commit()

        retriever = KnowledgeFts5Retriever(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        results = retriever._search_artifacts(cursor, "关于考量GPU利用率的指标的文档，帮我梳理一下", top_k=5, entity_terms=None)
        conn.close()

        doc_keys = [chunk.doc_key for chunk in results]
        assert "document_url:https://docs.example/container-gpu" in doc_keys
        assert "bake_knowledge:229" not in doc_keys

    def test_artifact_query_plan_keeps_rare_identifier_across_generic_word_variants(self, tmp_path):
        db_path = str(tmp_path / "artifact-query-plan.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                linked_knowledge_ids TEXT NOT NULL DEFAULT '[]',
                deleted_at INTEGER,
                updated_at INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO bake_documents
                (id, title, doc_type, summary, full_content, source_url, updated_at)
            VALUES
                (80, '容器云 GPU 指标采集项目', '技术文档',
                 'SMACT 与 SMOCC 指标说明', 'SMACT 用于衡量空分利用率。',
                 'https://docs.example/smact', 1000)
            """
        )
        conn.executemany(
            """
            INSERT INTO bake_documents
                (id, title, doc_type, summary, full_content, source_url, updated_at)
            VALUES (?, ?, '技术文档', '通用指标资料', '这是通用指标文档和项目方案。',
                    ?, ?)
            """,
            [
                (
                    index,
                    f"项目指标文档 {index}",
                    f"https://docs.example/generic-{index}",
                    2000 + index,
                )
                for index in range(100, 140)
            ],
        )
        conn.commit()
        conn.row_factory = sqlite3.Row

        plan = build_artifact_query_plan(
            conn.cursor(),
            "帮我找 SMACT 相关资料",
        )
        assert [term.text for term in plan.discriminative_terms] == ["smact"]
        assert "资料" in [term.text for term in plan.type_terms]
        assert {"帮我", "相关"}.issubset(plan.instruction_terms)

        generic_plan = build_artifact_query_plan(conn.cursor(), "GPU 指标")
        assert "gpu" in [term.text for term in generic_plan.discriminative_terms]
        assert "指标" in [term.text for term in generic_plan.generic_terms]

        retriever = KnowledgeFts5Retriever(db_path)
        for query in ("SMACT", "SMACT文档", "帮我找 SMACT 相关资料"):
            results = retriever._search_artifacts(
                conn.cursor(),
                query,
                top_k=5,
                entity_terms=None,
            )
            assert results[0].doc_key == "document_url:https://docs.example/smact"
        conn.close()

    def test_fts5_retriever_falls_back_to_app_name_match(self, tmp_path):
        db_path = str(tmp_path / "captures.db")
        _init_captures_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, input_text, audio_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1_710_000_000_000, "Google Chrome", "Claude", "", "", "", ""),
        )
        conn.commit()
        conn.close()

        retriever = Fts5Retriever(db_path)
        results = retriever.search("我最近用Google Chrome了吗", top_k=5, start_ts=1_700_000_000_000)
        assert len(results) == 1
        assert results[0].metadata["app_name"] == "Google Chrome"
        assert results[0].doc_key == "capture:1"

    def test_fts5_retriever_respects_recent_time_filter(self, tmp_path):
        db_path = str(tmp_path / "captures.db")
        _init_captures_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, input_text, audio_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1_600_000_000_000, "Google Chrome", "Old", "", "", "", ""),
                (2, 1_710_000_000_000, "Google Chrome", "Recent", "", "", "", ""),
            ],
        )
        conn.commit()
        conn.close()

        retriever = Fts5Retriever(db_path)
        results = retriever.search("Chrome", top_k=5, start_ts=1_700_000_000_000)
        assert [chunk.capture_id for chunk in results] == [2]

    def test_knowledge_retriever_filters_history_view_and_activity_type(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        _init_knowledge_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO timelines (id, capture_id, summary, overview, details, start_time, end_time, duration_minutes, frag_app_name, frag_win_title, entities, category, user_verified, observed_at, event_time_start, event_time_end, history_view, content_origin, activity_type, is_self_generated, evidence_strength) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 100, "今天问 Gemini", "今天问 Gemini 发布计划", "确认发布时间", 1_710_000_000_000, 1_710_000_060_000, 1, "Gemini", "Gemini", "[]", "聊天", 1, 1_710_000_060_000, None, None, 0, "live_interaction", "ask_ai", 0, "high"),
                (2, 101, "回看历史消息", "今天回看昨天飞书消息", "确认昨天安排", 1_710_000_100_000, 1_710_000_160_000, 1, "Feishu", "项目群", "[]", "聊天", 1, 1_710_000_160_000, 1_709_913_600_000, 1_709_914_000_000, 1, "historical_content", "reviewing_history", 0, "high"),
            ],
        )
        conn.commit()
        conn.close()

        retriever = KnowledgeFts5Retriever(db_path)
        ask_ai_results = retriever.search(
            "Gemini",
            top_k=5,
            observed_start_ts=1_710_000_000_000,
            observed_end_ts=1_710_000_200_000,
            activity_types=["ask_ai"],
            history_view=False,
            is_self_generated=False,
            evidence_strengths=["medium", "high"],
        )
        assert [chunk.metadata["knowledge_id"] for chunk in ask_ai_results] == [1]

        history_results = retriever.search(
            "飞书",
            top_k=5,
            observed_start_ts=1_710_000_000_000,
            observed_end_ts=1_710_000_200_000,
            activity_types=["reviewing_history", "chat", "reading"],
            content_origins=["historical_content"],
            history_view=True,
            is_self_generated=False,
            evidence_strengths=["medium", "high"],
        )
        assert [chunk.metadata["knowledge_id"] for chunk in history_results] == [2]


    def test_knowledge_retriever_filters_noise_overview(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        _init_knowledge_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO timelines (id, capture_id, summary, overview, details, start_time, end_time, duration_minutes, frag_app_name, frag_win_title, entities, category, user_verified, observed_at, event_time_start, event_time_end, history_view, content_origin, activity_type, is_self_generated, evidence_strength) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 100, "低价值条目", "低价值工作片段（invalid_json）", "噪声", 1_710_000_000_000, 1_710_000_060_000, 1, "Gemini", "Gemini", "[]", "其他", 0, 1_710_000_060_000, None, None, 0, "other", "other", 0, "low"),
                (2, 101, "AIGC 方案", "推进 AIGC 选题与页面方案", "整理 AIGC 工作流与页面方案", 1_710_000_100_000, 1_710_000_160_000, 1, "VS Code", "AIGC", "[]", "代码", 1, 1_710_000_160_000, None, None, 0, "live_interaction", "coding", 0, "high"),
            ],
        )
        conn.commit()
        conn.close()

        retriever = KnowledgeFts5Retriever(db_path)
        results = retriever.search("我最近关于aigc的工作有哪些", top_k=5, entity_terms=["aigc"])
        assert [chunk.metadata["knowledge_id"] for chunk in results] == [2]


    def test_knowledge_retriever_falls_back_to_frag_app_name_match(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        _init_knowledge_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO timelines (id, capture_id, summary, overview, details, start_time, end_time, duration_minutes, frag_app_name, frag_win_title, entities, category, user_verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 100, "浏览器工作", "整理资料", "在 Chrome 中查看文档", 1_710_000_000_000, 1_710_000_060_000, 1, "Google Chrome", "Claude", "[]", "文档", 1),
        )
        conn.commit()
        conn.close()

        retriever = KnowledgeFts5Retriever(db_path)
        results = retriever.search("我最近用Google Chrome了吗", top_k=5, start_ts=1_700_000_000_000)
        assert len(results) == 1
        assert results[0].metadata["app_name"] == "Google Chrome"
        assert results[0].doc_key == "knowledge:1"
        assert results[0].metadata["knowledge_id"] == 1


class TestDurableMemoryMaterialization:
    def test_timeline_hit_becomes_bake_knowledge_and_unmapped_timeline_is_dropped(self, tmp_path):
        db_path = str(tmp_path / "durable-knowledge.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE bake_knowledge (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT,
                detailed_content TEXT,
                entities TEXT,
                timeline_id INTEGER,
                source_timeline_ids TEXT DEFAULT '[]',
                source_capture_ids TEXT NOT NULL DEFAULT '[]',
                importance INTEGER DEFAULT 3,
                user_verified BOOLEAN DEFAULT 0,
                updated_at_ms INTEGER
            );
            CREATE TABLE bake_artifact_source_links (
                artifact_kind TEXT NOT NULL,
                artifact_id INTEGER NOT NULL,
                source_timeline_id INTEGER NOT NULL
            );
            INSERT INTO bake_knowledge
                (id, title, summary, content, detailed_content, entities,
                 timeline_id, source_timeline_ids, updated_at_ms)
            VALUES
                (2467, '电商 AI 模型效率方案', 'SMACT 指标长期结论',
                 'SMACT 用于衡量空分利用率', '', '[]', 5341, '[5341]', 1000);
            INSERT INTO bake_artifact_source_links
                (artifact_kind, artifact_id, source_timeline_id)
            VALUES ('knowledge', 2467, 5341);
            """
        )
        conn.commit()
        conn.close()
        hits = [
            _chunk(1, score=0.8, source="vector", doc_key="knowledge:5341", metadata={"source_type": "knowledge", "doc_key": "knowledge:5341", "knowledge_id": 5341}),
            _chunk(2, score=0.7, source="vector", doc_key="knowledge:3333", metadata={"source_type": "knowledge", "doc_key": "knowledge:3333", "knowledge_id": 3333}),
        ]

        results = KnowledgeFts5Retriever(db_path).materialize_durable_knowledge(
            hits, "SMACT 产品简介"
        )

        assert [chunk.doc_key for chunk in results] == ["bake_knowledge:2467"]
        assert results[0].score == pytest.approx(0.8)
        assert results[0].metadata["retrieval_method"] == "timeline_to_bake_knowledge"
        assert results[0].metadata["semantic_source_timeline_id"] == "5341"

    def test_data_latest_snapshot_is_a_regular_artifact(self, tmp_path):
        db_path = str(tmp_path / "data-memory.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE data_sources (
                id INTEGER PRIMARY KEY,
                canonical_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_url TEXT,
                access_mode TEXT NOT NULL,
                refresh_policy TEXT NOT NULL,
                realtime_level TEXT NOT NULL,
                source_app_name TEXT,
                source_window_title TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                last_collected_at INTEGER,
                last_success_at INTEGER,
                last_error_code TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_at INTEGER
            );
            CREATE TABLE data_snapshots (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL,
                collected_at INTEGER NOT NULL,
                observed_at INTEGER,
                collector TEXT NOT NULL,
                content_text TEXT NOT NULL,
                structured_data TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL,
                freshness_ttl_seconds INTEGER NOT NULL DEFAULT 0,
                provenance TEXT NOT NULL DEFAULT '{}',
                source_capture_ids TEXT NOT NULL DEFAULT '[]',
                source_timeline_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT INTO data_sources
                (id, canonical_key, title, source_kind, access_mode, refresh_policy,
                 realtime_level, first_seen_at, last_seen_at, status, created_at, updated_at)
            VALUES
                (7, 'memory:gpu', '电商 GPU 信息平台', 'work_memory', 'memory_only',
                 'never', 'observed', 1, 2, 'active', 1, 2);
            INSERT INTO data_snapshots
                (id, source_id, collected_at, observed_at, collector, content_text,
                 structured_data, content_hash, status, created_at)
            VALUES
                (70, 7, 1000, 900, 'memory_extract', 'GPU 利用率为 42%',
                 '{"metric":"GPU 利用率","value":"42%"}', 'old', 'success', 1000),
                (71, 7, 2000, 1900, 'memory_extract', 'GPU 利用率为 55%',
                 '{"metric":"GPU 利用率","value":"55%"}', 'new', 'success', 2000);
            """
        )
        conn.commit()
        conn.close()

        results = KnowledgeFts5Retriever(db_path).search("GPU 利用率数据", top_k=5)

        data_results = [chunk for chunk in results if chunk.doc_key == "data:7"]
        assert len(data_results) == 1
        assert "55%" in data_results[0].text
        assert "42%" not in data_results[0].text
        assert data_results[0].metadata["source_type"] == "data"
        assert data_results[0].metadata["snapshot_id"] == 71


class TestOllamaBackend:
    def test_model_name(self):
        backend = OllamaBackend(model="qwen2.5:7b")
        assert backend.model_name == "qwen2.5:7b"

    def test_is_available_returns_bool(self):
        backend = OllamaBackend()
        # Ollama 不一定运行，但不应抛异常
        assert isinstance(backend.is_available(), bool)

    def test_default_model(self):
        backend = OllamaBackend()
        assert "qwen" in backend.model_name.lower()


class TestAttachDocumentLinks:
    """咨询答案中提及的文档应被补上超链接。"""

    @staticmethod
    def _chunk(title=None, url=None, text="", source_type="document"):
        return RetrievedChunk(
            capture_id=0,
            text=text,
            score=1.0,
            source=source_type,
            doc_key=f"document:{title or text[:8]}",
            metadata={
                "source_type": source_type,
                "title": title,
                "source_url": url,
            },
        )

    def test_normalize_doc_title_strips_cloud_doc_suffix(self):
        assert _normalize_doc_title("《稳柱 - 收入巡检归因复盘》") == "稳柱-收入巡检归因复盘"
        assert _normalize_doc_title("稳柱-收入巡检归因复盘 - 云文档") == "稳柱-收入巡检归因复盘"
        assert _normalize_doc_title("某文档（云文档）") == "某文档"

    def test_inline_replaces_book_title_mention(self):
        chunks = [
            self._chunk(
                title="稳柱-收入巡检归因复盘 - 云文档",
                url="https://docs.example.com/d/home/abc",
            ),
        ]
        answer = "最相关的是《稳柱 - 收入巡检归因复盘》，记录了故障复盘。"
        result = _attach_document_links(answer, chunks)
        assert "[《稳柱 - 收入巡检归因复盘》](https://docs.example.com/d/home/abc)" in result

    def test_mentioned_doc_without_mention_marker_appends_section(self):
        chunks = [
            self._chunk(
                title="稳柱-收入巡检归因复盘 - 云文档",
                url="https://docs.example.com/d/home/abc",
            ),
        ]
        answer = "最相关的是 稳柱-收入巡检归因复盘，记录了故障复盘。"
        result = _attach_document_links(answer, chunks)
        assert "相关文档链接：" in result
        assert "- [稳柱-收入巡检归因复盘 - 云文档](https://docs.example.com/d/home/abc)" in result

    def test_unmentioned_document_not_linked(self):
        chunks = [
            self._chunk(title="另一篇文档", url="https://docs.example.com/other"),
        ]
        answer = "本次只讨论《稳柱 - 收入巡检归因复盘》。"
        assert _attach_document_links(answer, chunks) == answer

    def test_existing_url_not_duplicated(self):
        chunks = [
            self._chunk(
                title="稳柱-收入巡检归因复盘",
                url="https://docs.example.com/d/home/abc",
            ),
        ]
        answer = "文档地址已给出：https://docs.example.com/d/home/abc （《稳柱-收入巡检归因复盘》）"
        assert _attach_document_links(answer, chunks) == answer

    def test_collects_url_from_chunk_text(self):
        chunks = [
            self._chunk(
                text="文档：万擎top10模型数据 - 云文档"
                "\nURL：https://docs.example.com/sheet/1",
            ),
        ]
        links = _collect_document_links(chunks)
        assert links == [("万擎top10模型数据 - 云文档", "https://docs.example.com/sheet/1")]

    def test_baked_document_lookup_fills_missing_url(self, tmp_path):
        db_path = str(tmp_path / "memory-bread.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE bake_documents ("
            "id INTEGER PRIMARY KEY, title TEXT, source_url TEXT, deleted_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO bake_documents (title, source_url, deleted_at) VALUES (?, ?, NULL)",
            ("MaaS的一些联想 - 云文档", "https://docs.example.com/d/home/maas"),
        )
        conn.commit()
        conn.close()

        # 阅读时间线片段自身没有 URL，应按烘焙文档标题索引兜底补链接。
        chunks = [
            self._chunk(title="MaaS的一些联想", source_type="knowledge"),
        ]
        answer = "你看过《MaaS的一些联想》。"
        result = _attach_document_links(answer, chunks, db_path=db_path)
        assert "[《MaaS的一些联想》](https://docs.example.com/d/home/maas)" in result

    @staticmethod
    def _seed_baked_documents(db_path, rows):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE bake_documents ("
            "id INTEGER PRIMARY KEY, title TEXT, source_url TEXT, deleted_at INTEGER)"
        )
        for index, (title, url) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO bake_documents (id, title, source_url, deleted_at) VALUES (?, ?, ?, NULL)",
                (index, title, url),
            )
        conn.commit()
        conn.close()

    def test_baked_suffix_match_only_when_unique(self, tmp_path):
        db_path = str(tmp_path / "memory-bread.db")
        self._seed_baked_documents(
            db_path,
            [("2026-07-21 商业化收入波动自检报告", "https://docs.example.com/d/0721")],
        )
        index = {"2026-07-21商业化收入波动自检报告": ("2026-07-21 商业化收入波动自检报告", "https://docs.example.com/d/0721")}
        assert _lookup_baked_mention(index, "商业化收入波动自检报告") is not None

        index["2026-07-24商业化收入波动自检报告"] = ("2026-07-24 商业化收入波动自检报告", "https://docs.example.com/d/0724")
        # 多个同名日报无法确定具体版本，不得误链。
        assert _lookup_baked_mention(index, "商业化收入波动自检报告") is None

        answer = "已完成《商业化收入波动自检报告》测试。"
        result = _attach_document_links(answer, [], db_path=db_path)
        assert "[《商业化收入波动自检报告》](https://docs.example.com/d/0721)" in result

    def test_broken_mention_falls_back_to_section(self, tmp_path):
        db_path = str(tmp_path / "memory-bread.db")
        self._seed_baked_documents(
            db_path,
            [("[进度日报]智能应急处置归因 - 稳柱产品", "https://docs.example.com/d/533")],
        )
        # 模型输出的标题缺少开头书名号，无法原位替换，应兜底到文末链接列表。
        answer = "工作汇报类：[进度日报]智能应急处置归因 - 稳柱产品》（云文档）。"
        result = _attach_document_links(answer, [], db_path=db_path)
        assert "相关文档链接：" in result
        assert "- [[进度日报]智能应急处置归因 - 稳柱产品](https://docs.example.com/d/533)" in result
