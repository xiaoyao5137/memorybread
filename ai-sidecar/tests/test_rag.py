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
from typing import Optional

import pytest

from embedding.base  import EmbeddingBackend, EmbeddingVector
from embedding.model import EmbeddingModel
from rag.llm.base    import LlmBackend, LlmResponse
from rag.llm.ollama  import OllamaBackend
from rag.pipeline    import RagPipeline, RagResult, _normalize_evidence_references, _normalize_weekly_report
from rag.query_planner import build_artifact_query_plan
from rag.retriever   import (
    Fts5Retriever,
    KnowledgeFts5Retriever,
    RetrievedChunk,
    VectorRetriever,
    VectorSearchFilter,
    _bounded_fallback_terms,
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
        query_mode: str = "lookup",
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
            "query_mode": query_mode,
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


# ── RagPipeline ───────────────────────────────────────────────────────────────

class TestRagPipeline:
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
            _chunk(1, 0.9, source="knowledge", doc_key="knowledge:1", metadata={"source_type": "knowledge", "doc_key": "knowledge:1", "knowledge_id": 1}),
            _chunk(2, 0.7, source="knowledge", doc_key="knowledge:2", metadata={"source_type": "knowledge", "doc_key": "knowledge:2", "knowledge_id": 2}),
        ]
        pipeline = _make_pipeline(knowledge_chunks=knowledge)
        result = pipeline.query("工作内容")
        assert len(result.contexts) >= 1

    def test_model_in_result(self):
        pipeline = _make_pipeline()
        result   = pipeline.query("问题")
        assert result.model == "mock-llm"

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
        assert "gemini" in (filters.app_names or [])

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
        assert knowledge.last_kwargs["activity_types"] == ["ask_ai"]
        assert knowledge.last_kwargs["history_view"] is False
        assert knowledge.last_kwargs["is_self_generated"] is False
        assert knowledge.last_kwargs["evidence_strengths"] == ["medium", "high"]
        filters = vector_r.last_kwargs["filters"]
        assert filters.observed_start_ts is not None
        assert filters.activity_types == ["ask_ai"]
        assert filters.history_view is False
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
        assert knowledge.last_kwargs["history_view"] is True
        assert knowledge.last_kwargs["content_origins"] == ["historical_content"]
        assert knowledge.last_kwargs["activity_types"] == ["reviewing_history", "chat", "reading"]
        filters = vector_r.last_kwargs["filters"]
        assert filters.history_view is True
        assert filters.content_origins == ["historical_content"]

    def test_query_intent_applies_recent_summary_mode(self):
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
        assert knowledge.last_kwargs["query_mode"] == "summary"
        assert "aigc" in (knowledge.last_kwargs["entity_terms"] or [])
        filters = vector_r.last_kwargs["filters"]
        assert filters.app_names in (None, [])

    def test_weekly_report_intent_detected(self):
        """'帮我写工作周报' 应识别为 weekly_report 任务型意图"""
        intent = RagPipeline._parse_query_intent("帮我写下我的工作周报")
        assert intent.task_type == "weekly_report"
        assert intent.query_mode == "summary"
        assert intent.observed_start_ts is not None  # 默认本周
        # 报告任务在召回阶段不过滤活动类型，由 _select_contexts 做精细筛选。
        assert intent.activity_types == []
        assert intent.evidence_strengths == []
        assert intent.history_view is None

    def test_daily_report_intent_detected(self):
        """'帮我写今天的日报' 应识别为 daily_report 任务型意图"""
        intent = RagPipeline._parse_query_intent("帮我写今天的工作日报")
        assert intent.task_type == "daily_report"
        assert intent.query_mode == "summary"
        assert intent.observed_start_ts is not None  # 今天

    def test_weekly_report_last_week(self):
        """'帮我写上周周报' 应识别为 weekly_report + 上周时间范围"""
        intent = RagPipeline._parse_query_intent("帮我写上周的工作周报")
        assert intent.task_type == "weekly_report"
        # 上周 end_ts 应早于本周开始
        from rag.pipeline import _week_start_ms
        this_week_start = _week_start_ms()
        assert intent.observed_end_ts is not None
        assert intent.observed_end_ts < this_week_start

    def test_weekly_report_uses_large_top_k(self):
        """周报查询应自动扩大 top_k 到 18"""
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
        pipeline.query("帮我写工作周报")
        # weekly_report 分支会将 effective_top_k 拉到至少 18，因此 knowledge 检索 top_k 至少是 36
        assert knowledge.last_kwargs["top_k"] >= 36

    def test_project_weekly_report_intent_and_kpi_mode(self):
        intent = RagPipeline._parse_query_intent("帮我生成本周项目周报，包含OKR和KPI进展")
        assert intent.task_type == "project_weekly_report"
        assert intent.kpi_mode is True
        assert intent.query_mode == "summary"

    def test_weekly_report_kpi_mode_sets_flag(self):
        intent = RagPipeline._parse_query_intent("帮我写工作周报，重点看OKR达成率")
        assert intent.task_type == "weekly_report"
        assert intent.kpi_mode is True

    def test_project_weekly_report_prompt_requires_quant_section(self):
        evidence_chunk = RetrievedChunk(
            capture_id=34,
            text="本周完成 3 项核心需求，KPI 达成率 75%，上线 2 个接口",
            score=0.95,
            source="knowledge",
            doc_key="knowledge:12",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:12",
                "knowledge_id": 12,
                "capture_id": 34,
                "importance": 5,
                "user_verified": 1,
                "evidence_strength": "high",
                "activity_type": "coding",
            },
        )
        llm = MockLlmBackend(model_name="qwen2.5:3b")
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[evidence_chunk]),  # type: ignore[arg-type]
            llm=llm,
        )

        pipeline.query("帮我生成项目周报，包含OKR/KPI/专项进展")
        assert "## 本周量化进展（OKR/KPI/专项）" in llm.last_prompt
        assert "【量化证据】（仅可引用以下证据中的数字结论）" in llm.last_prompt
        assert "证据：R#1" in llm.last_prompt
        assert "K#12/C#34" not in llm.last_prompt

    def test_quant_evidence_extractor_filters_noise_numbers(self):
        candidate = (
            "2026-04-15 查看文档 v1.2.3；完成 3 项接口联调，成功率 99%，耗时 30 分钟；"
            "工单编号 12345"
        )
        lines = RagPipeline._extract_quant_fact_lines(candidate, kpi_mode=False)
        assert any("完成 3 项接口联调" in line for line in lines)
        assert all("2026-04-15" not in line for line in lines)

    def test_quant_evidence_block_uses_best_evidence(self, tmp_path):
        db_path = str(tmp_path / "captures.db")
        _init_knowledge_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.executemany(
            """
            INSERT INTO timelines (
                id, capture_id, summary, overview, details, start_time, end_time, duration_minutes,
                frag_app_name, frag_win_title, entities, category, user_verified, observed_at,
                event_time_start, event_time_end, history_view, content_origin, activity_type,
                is_self_generated, evidence_strength
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    21, 101, "", "本周完成 4 项需求", "本周完成 4 项需求，KPI 达成率 80%", 1_710_000_000_000,
                    1_710_000_600_000, 60, "IDE", "", "[]", "开发", 1, 1_710_000_600_000,
                    1_710_000_000_000, 1_710_000_600_000, 0, "live_interaction", "coding", 0, "high"
                ),
                (
                    22, 102, "", "本周完成 4 项需求", "本周完成 4 项需求，KPI 达成率 80%", 1_709_000_000_000,
                    1_709_000_600_000, 60, "IDE", "", "[]", "开发", 0, 1_709_000_600_000,
                    1_709_000_000_000, 1_709_000_600_000, 0, "live_interaction", "coding", 0, "low"
                ),
            ],
        )
        conn.commit()
        conn.close()

        chunk_high = RetrievedChunk(
            capture_id=101,
            text="本周完成 4 项需求，KPI 达成率 80%",
            score=0.9,
            source="knowledge",
            doc_key="knowledge:21",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:21",
                "knowledge_id": 21,
                "capture_id": 101,
                "importance": 5,
                "user_verified": 1,
                "evidence_strength": "high",
                "observed_at": 1_710_000_600_000,
                "activity_type": "coding",
            },
        )
        chunk_low = RetrievedChunk(
            capture_id=102,
            text="本周完成 4 项需求，KPI 达成率 80%",
            score=0.88,
            source="knowledge",
            doc_key="knowledge:22",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:22",
                "knowledge_id": 22,
                "capture_id": 102,
                "importance": 2,
                "user_verified": 0,
                "evidence_strength": "low",
                "observed_at": 1_709_000_600_000,
                "activity_type": "coding",
            },
        )

        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=MockFts5Retriever(chunks=[chunk_high, chunk_low]),  # type: ignore[arg-type]
            llm=MockLlmBackend(),
            db_path=db_path,
        )

        block = pipeline._build_quant_evidence_block([chunk_high, chunk_low], kpi_mode=True, top_n=3)
        assert "【量化证据】" in block
        assert "R#1" in block
        assert "K#21/C#101" not in block
        assert "K#22/C#102" not in block

    def test_weekly_report_normalizes_internal_evidence_refs(self):
        chunk = RetrievedChunk(
            capture_id=101,
            text="完成 4 项需求",
            score=0.9,
            source="knowledge",
            doc_key="knowledge:21",
            metadata={
                "source_type": "knowledge",
                "doc_key": "knowledge:21",
                "knowledge_id": 21,
                "capture_id": 101,
            },
        )

        answer = "## 本周核心产出\n- **修复问题**：完成 4 项需求（证据：K#21/C#101）。"
        normalized = _normalize_evidence_references(answer, [chunk])
        assert "证据：R#1" in normalized
        assert "K#21/C#101" not in normalized

    def test_weekly_report_drops_leaked_evidence_appendix(self):
        answer = "## 本周核心产出\n- **修复问题**：完成 4 项需求。\n\n依据证据\n- [1] 完成 4 项需求"
        normalized = _normalize_weekly_report(answer)
        assert "修复问题" in normalized
        assert "依据证据" not in normalized
        assert "[1] 完成 4 项需求" not in normalized

    def test_weekly_report_system_prompt_used(self):
        """周报任务应使用专属 system prompt，而非默认 prompt"""
        knowledge = MockFts5Retriever(chunks=[_chunk(1, source="knowledge")])
        llm = MockLlmBackend(model_name="qwen2.5:3b")
        pipeline = RagPipeline(
            embedding_model=EmbeddingModel(backend=MockEmbeddingBackend()),
            vector_retriever=MockVectorRetriever(),  # type: ignore[arg-type]
            fts5_retriever=MockFts5Retriever(),      # type: ignore[arg-type]
            knowledge_retriever=knowledge,           # type: ignore[arg-type]
            llm=llm,
        )
        pipeline.query("帮我写工作周报")
        assert "周报" in llm.last_system
        assert "activity_type" in llm.last_system

    def test_weekly_report_passes_empty_entity_terms_to_knowledge(self):
        """周报任务应传空 entity_terms，实现宽松全量时间段召回"""
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
        pipeline.query("帮我写工作周报")
        # 任务型意图不按关键词过滤
        assert knowledge.last_kwargs.get("entity_terms") is None

    def test_non_write_intent_not_treated_as_report(self):
        """纯浏览型查询不应识别为任务型意图"""
        intent = RagPipeline._parse_query_intent("本周工作总结是什么")
        assert intent.task_type is None

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
                _chunk(3, source="knowledge", doc_key="knowledge:3", metadata={"source_type": "knowledge", "doc_key": "knowledge:3", "knowledge_id": 3})
            ]),  # type: ignore[arg-type]
            llm=llm,
            top_k=3,
        )
        pipeline.query("Gemini")
        first_context_line = llm.last_prompt.splitlines()[1]
        assert "[knowledge]" in first_context_line

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

    def test_embedding_failure_degrades_gracefully(self):
        """embedding 失败应降级为纯 knowledge 检索，不抛异常"""
        knowledge = [
            _chunk(1, 0.8, source="knowledge", doc_key="knowledge:1", metadata={"source_type": "knowledge", "doc_key": "knowledge:1", "knowledge_id": 1})
        ]
        pipeline = _make_pipeline(
            knowledge_chunks=knowledge,
            embed_raise=RuntimeError("embedding 服务不可用"),
        )
        result = pipeline.query("工作记录")
        assert result.answer is not None
        assert len(result.contexts) >= 1
        assert all(chunk.metadata.get("source_type") == "knowledge" for chunk in result.contexts)

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
            query_mode="lookup",
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
        assert promoted[0].metadata["promoted_by_knowledge_ids"] == ["1884"]

    def test_artifact_search_returns_document_and_knowledge_for_gpu_metric_doc_query(self, tmp_path):
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
        assert "bake_knowledge:229" in doc_keys

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
        results = retriever.search("我最近关于aigc的工作有哪些", top_k=5, entity_terms=["aigc"], query_mode="summary")
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
