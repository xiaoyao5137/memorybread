from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from creation.service import CreationOptions, CreationService


def document_408() -> dict:
    return {
        "id": 408,
        "title": "示例公司员工周年礼物领取指南",
        "doc_type": "资料参考",
        "summary": "面向员工周年场景的礼物领取参考资料",
        "full_content": "员工可按周年节点自主选择并领取礼物。",
        "sections_json": "[]",
        "style_phrases": "[]",
        "prompt_hint": "适用于员工周年礼物与领取指南类创作",
        "usage_count": 0,
        "review_status": "auto_created",
        "updated_at": 1_785_081_300_661,
        "source_url": None,
    }


def test_vector_evidence_survives_keyword_dedup_and_relevance_filter(tmp_path):
    service = CreationService.__new__(CreationService)
    service.db_path = str(tmp_path / "memory-bread.db")
    tmp_path.joinpath("memory-bread.db").touch()
    service.enable_vector_recall = True
    service._embedding_model = object()
    service._query_document_rows = lambda *_args: [document_408()]
    service._vector_recall = lambda *_args: [
        {**document_408(), "_vector_similarity": 0.82}
    ]

    references = service.retrieve_references(
        "写一份周年员工的礼物指南",
        {
            "doc_type": "指南",
            "keywords": ["写一份周年员", "礼物指南"],
        },
        CreationOptions(max_references=6),
    )

    assert [item.id for item in references] == [408]
    assert references[0].relevance_score == 0.82


def test_semantic_recall_reranks_every_memory_domain_without_query_synonyms(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bake_documents VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 0, "
        "'auto_created', ?, ?, NULL)",
        (
            41,
            "切换保障资料",
            "技术文档",
            "生产割接前的核验与回退说明",
            "生产割接前完成依赖核验并准备回退方案。",
            now_ms,
            "https://docs.example/cutover",
        ),
    )
    conn.execute(
        "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, '', 5, 1, ?, ?)",
        (
            42,
            "交付风险记录",
            "依赖核验仍有两项待关闭",
            "依赖核验仍有两项待关闭",
            "依赖核验仍有两项待关闭",
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        "INSERT INTO bake_sops VALUES (?, ?, ?, ?, ?, '', 5, 1, ?, ?)",
        (
            43,
            "灰度切流手册",
            "先小流量验证再扩大范围",
            "先小流量验证再扩大范围",
            "先小流量验证再扩大范围",
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        "INSERT INTO data_sources VALUES (?, ?, ?, 'active', NULL)",
        (44, "回退演练指标", "https://bi.example/cutover"),
    )
    conn.execute(
        "INSERT INTO data_snapshots VALUES (?, ?, ?, ?, ?, '{}', 'success', ?, ?)",
        (45, 44, now_ms, now_ms, "回退演练成功率 98%", now_ms, now_ms),
    )
    conn.commit()
    conn.close()

    class Vector:
        def __init__(self):
            self.vector = [1.0, 0.0]

    class SemanticModel:
        def encode(self, texts):
            return [Vector() for _ in texts]

    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)
    service.enable_vector_recall = True
    service._embedding_model = SemanticModel()
    query = "整理客户迁移的上线准备情况"
    requirement = service.analyze_requirement(query, CreationOptions())

    references = service.retrieve_references(
        query,
        requirement,
        CreationOptions(max_references=10),
    )

    identities = {(item.source_type, item.source_id) for item in references}
    assert identities >= {
        ("document", 41),
        ("knowledge", 42),
        ("operation", 43),
        ("data", 44),
    }


def test_chinese_keyword_fallback_and_doc_type_cover_gift_guide():
    service = CreationService.__new__(CreationService)

    keywords = service._extract_keywords("写一份周年员工的礼物指南")

    assert any("周年" in keyword for keyword in keywords)
    assert any("礼物" in keyword for keyword in keywords)
    assert service._infer_doc_type("写一份周年员工的礼物指南") == "指南"


def test_relevance_fuses_independent_lexical_and_semantic_evidence():
    service = CreationService.__new__(CreationService)
    row = {
        "title": "灰度发布检查",
        "doc_type": "knowledge",
        "summary": "切流前完成依赖核验",
        "full_content": "",
        "sections_json": "[]",
        "prompt_hint": "",
        "_vector_similarity": 0.75,
    }

    score = service._score_relevance(
        row,
        {"keywords": ["灰度发布", "上线准备"], "doc_type": ""},
    )

    assert score > 0.75
    assert score <= 1.0


def test_distinctive_step_phrase_keeps_h3_optimization_knowledge_relevant():
    service = CreationService.__new__(CreationService)
    row = {
        "title": "Minimax-H3 步数蒸馏优化进度",
        "doc_type": "knowledge",
        "summary": "量化、稀疏注意力、通信和 Pipeline 模块均已记录当前状态。",
        "full_content": "本周继续推进推理性能优化，并完成各模块性能指标验证。",
        "sections_json": "[]",
        "prompt_hint": "",
    }

    score = service._score_relevance(
        row,
        {
            "keywords": ["AIGC", "共建项目", "进展", "共建项目周报", "推理性能优化"],
            "doc_type": "",
        },
    )

    assert score >= 0.4


def test_top_k_diversifies_memory_domains_and_backfills_when_needed():
    class Reference:
        def __init__(self, source_type, source_id):
            self.source_type = source_type
            self.source_id = source_id

    ranked = [Reference("document", index) for index in range(8)]
    ranked.extend(Reference("knowledge", index) for index in range(8, 13))

    selected = CreationService._select_diverse_references(ranked, 10)
    selected_domains = [item.source_type for item in selected]
    documents_only = CreationService._select_diverse_references(ranked[:8], 8)

    assert selected_domains.count("document") == 5
    assert selected_domains.count("knowledge") == 5
    assert len(documents_only) == 8


def test_top_k_gives_knowledge_a_slightly_larger_quota():
    class Reference:
        def __init__(self, source_type, source_id):
            self.source_type = source_type
            self.source_id = source_id

    # knowledge 在前时，其配额（0.6）决定能进入 Top-K 的上限
    ranked = [Reference("knowledge", index) for index in range(8)]
    ranked.extend(Reference("document", index + 8) for index in range(8))

    selected = CreationService._select_diverse_references(ranked, 10)
    selected_domains = [item.source_type for item in selected]

    assert selected_domains.count("knowledge") == 6
    assert selected_domains.count("document") == 4


def test_skill_objective_keywords_keep_aigc_project_and_inference_topic():
    service = CreationService.__new__(CreationService)

    keywords = service._extract_keywords(
        "用@记忆搜索 Tool 工具获取本周AIGC共建项目的进展，以及及AIGC "
        "共建项目周报里关于推理性能优化相关的进展"
    )

    assert "AIGC" in keywords
    assert "共建项目" in keywords
    assert "共建项目周报" in keywords
    assert "推理性能优化" in keywords


def test_current_week_is_resolved_to_runtime_iso_week_and_exact_dates():
    now = datetime(2026, 8, 13, 20, 20, tzinfo=timezone(timedelta(hours=8)))

    context = CreationService._relative_time_context("生成本周周报", now=now)

    assert context["iso_year"] == 2026
    assert context["iso_week"] == 33
    assert context["period_start"] == "2026-08-10"
    assert context["period_end"] == "2026-08-16"
    assert context["display"] == "2026年第33周（2026-08-10 至 2026-08-16）"


def _create_unified_memory_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE bake_documents (
            id INTEGER PRIMARY KEY, title TEXT, doc_type TEXT, summary TEXT,
            full_content TEXT, sections_json TEXT, style_phrases TEXT,
            prompt_hint TEXT, usage_count INTEGER, review_status TEXT,
            updated_at INTEGER, source_url TEXT, deleted_at INTEGER
        );
        CREATE TABLE bake_knowledge (
            id INTEGER PRIMARY KEY, title TEXT, summary TEXT, content TEXT,
            detailed_content TEXT, entities TEXT, importance INTEGER,
            user_verified INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER
        );
        CREATE TABLE bake_sops (
            id INTEGER PRIMARY KEY, title TEXT, summary TEXT, content TEXT,
            detailed_content TEXT, entities TEXT, importance INTEGER,
            user_verified INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER
        );
        CREATE TABLE data_sources (
            id INTEGER PRIMARY KEY, title TEXT, source_url TEXT, status TEXT,
            deleted_at INTEGER
        );
        CREATE TABLE data_snapshots (
            id INTEGER PRIMARY KEY, source_id INTEGER, collected_at INTEGER,
            observed_at INTEGER, content_text TEXT, structured_data TEXT,
            status TEXT, period_start_at INTEGER, period_end_at INTEGER
        );
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY, ts INTEGER, webpage_title TEXT,
            win_title TEXT, ax_text TEXT, ocr_text TEXT, input_text TEXT,
            audio_text TEXT, url TEXT, is_sensitive INTEGER
        );
        """
    )
    conn.close()


def test_unified_memory_recall_includes_document_knowledge_operation_and_data(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)
    service.enable_vector_recall = False
    service._embedding_model = None
    query = "本周 AIGC 共建项目周报 推理性能优化"
    requirement = service.analyze_requirement(query, CreationOptions())
    start_ms = requirement["time_context"]["period_start_ms"]
    end_ms = requirement["time_context"]["period_end_ms"]
    content = "AIGC 共建项目周报：本周完成推理性能优化并形成阶段进展。"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bake_documents VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 0, "
        "'auto_created', ?, ?, NULL)",
        (582, "AIGC 共建项目周报", "周报", content, content, now_ms, "https://docs.example/aigc"),
    )
    conn.execute(
        "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, '', 5, 1, ?, ?)",
        (2275, "AIGC 共建进展知识", content, content, content, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO bake_sops VALUES (?, ?, ?, ?, ?, '', 4, 1, ?, ?)",
        (688, "AIGC 推理性能优化操作", content, content, content, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO data_sources VALUES (?, ?, ?, 'active', NULL)",
        (214, "AIGC 推理性能看板", "https://bi.example/aigc"),
    )
    conn.execute(
        "INSERT INTO data_snapshots VALUES (?, ?, ?, ?, ?, '{}', 'success', ?, ?)",
        (1, 214, now_ms, now_ms, content, start_ms, end_ms),
    )
    conn.commit()
    conn.close()

    references = service.retrieve_references(
        query,
        requirement,
        CreationOptions(max_references=30),
    )

    identities = {(item.source_type, item.source_id) for item in references}
    assert ("document", 582) in identities
    assert ("knowledge", 2275) in identities
    assert ("operation", 688) in identities
    assert ("data", 214) in identities


def test_newer_pending_capture_is_used_before_linked_document_finishes_baking(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)
    service.enable_vector_recall = False
    service._embedding_model = None
    query = "本周 AIGC 共建项目周报 推理性能优化"
    requirement = service.analyze_requirement(query, CreationOptions())
    capture_ms = requirement["time_context"]["period_start_ms"] + 3_600_000
    content = ("AIGC 共建项目周报，本周推理性能优化已经完成。" * 30)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bake_documents VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 0, "
        "'auto_created', ?, ?, NULL)",
        (
            582,
            "AIGC 共建项目周报",
            "周报",
            content,
            content,
            capture_ms - 1_000,
            "https://docs.example/aigc",
        ),
    )
    conn.execute(
        "INSERT INTO captures VALUES (?, ?, ?, '', ?, '', '', '', ?, 0)",
        (
            33221,
            capture_ms,
            "AIGC 共建项目周报",
            content,
            "https://docs.example/aigc?ro=false",
        ),
    )
    conn.commit()
    conn.close()

    references = service.retrieve_references(
        query,
        requirement,
        CreationOptions(max_references=30),
    )

    linked = [
        item
        for item in references
        if item.source_url and "docs.example/aigc" in item.source_url
    ]
    assert len(linked) == 1
    assert linked[0].source_type == "pending_document"
    assert linked[0].source_id == 33221


@pytest.mark.asyncio
async def test_github_search_maps_public_repository_metadata(monkeypatch):
    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, params):
            assert url == "https://api.github.com/search/repositories"
            assert params["q"]
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "items": [
                        {
                            "full_name": "example/memory-agent",
                            "html_url": "https://github.com/example/memory-agent",
                            "description": "A public memory agent toolkit",
                            "stargazers_count": 321,
                            "language": "Python",
                            "updated_at": "2026-07-01T00:00:00Z",
                        }
                    ]
                },
            )

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService.__new__(CreationService)

    results = await service.search_github_context(
        "检索 GitHub 记忆 Agent 仓库",
        {"topic": "记忆 Agent", "keywords": ["memory", "agent"]},
    )

    assert len(results) == 1
    assert results[0].full_name == "example/memory-agent"
    assert results[0].stars == 321


def test_distilled_memory_domains_are_not_penalized_by_document_structure_scores():
    """提炼型记忆（知识/操作/数据）不得被长文档的结构化标准压低。

    否则与步骤主题强相关的知识（如 AIGC 周会项目进度）会因为
    completeness/format 两项被系统性扣分，从高相关度掉出 Top-K。
    """
    service = CreationService.__new__(CreationService)
    knowledge_row = {
        "source_type": "knowledge",
        "full_content": "AIGC 共建项目本周完成阶段性交付与性能验证，推理吞吐提升明显。",
        "sections_json": "[]",
        "style_phrases": "[]",
    }

    # 知识短正文也应有可用的完备度下限，格式给中性分而非惩罚。
    assert service._score_completeness(knowledge_row) >= 0.45
    assert service._score_format(knowledge_row, {}) == 0.55
    long_knowledge = dict(knowledge_row, full_content="进" * 1200)
    assert service._score_completeness(long_knowledge) == 1.0

    # 文档域继续按长文档结构标准评分，行为不变。
    document_row = {
        "source_type": "document",
        "full_content": "短正文",
        "sections_json": "[]",
        "style_phrases": "[]",
    }
    assert service._score_completeness(document_row) == 0.25
    assert service._score_format(document_row, {}) == 0.2

    # 操作、数据域同样豁免结构化惩罚。
    for domain in ("operation", "data"):
        row = dict(knowledge_row, source_type=domain)
        assert service._score_completeness(row) >= 0.45
        assert service._score_format(row, {}) == 0.55


def _insert_entity_reference_fixture(db_path):
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    direct_content = "Onepoint 是快手电商内部的知识工作平台，支持会议记录与知识检索。"
    conn.execute(
        "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?)",
        (
            9001,
            "Onepoint 产品定位与核心能力",
            direct_content,
            direct_content,
            direct_content,
            '["Onepoint"]',
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?)",
        (
            9002,
            "快手电商 AI 基座产品矩阵",
            "应用层包含团队知识产品",
            "Onepoint 位于应用与产品层。",
            "Onepoint 位于应用与产品层。",
            '["快手电商", "Onepoint"]',
            now_ms - 1000,
            now_ms - 1000,
        ),
    )
    conn.execute(
        "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, '', 5, 1, ?, ?)",
        (
            9003,
            "快手电商 AIGC 视频生成方案",
            "商品视频生成、审核与成本优化",
            "快手电商产品的 AIGC 生成链路。",
            "快手电商产品的 AIGC 生成链路。",
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        "INSERT INTO data_sources VALUES (?, ?, ?, 'active', NULL)",
        (9004, "用户浏览云文档主页，查看近期访问记录", "https://docs.example/home"),
    )
    conn.execute(
        "INSERT INTO data_snapshots VALUES (?, ?, ?, ?, ?, '{}', 'success', ?, ?)",
        (
            9005,
            9004,
            now_ms,
            now_ms,
            "云文档主页 最近访问 文档列表 " + "Onepoint 与大量其他产品条目。" * 80,
            now_ms,
            now_ms,
        ),
    )
    conn.commit()
    conn.close()


def test_requirement_promotes_only_corpus_verified_name_slot_to_primary_entity(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    _insert_entity_reference_fixture(db_path)
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)

    requirement = service.analyze_requirement(
        "请生成一份快手电商Onepoint产品的介绍文档",
        CreationOptions(),
    )
    generic = service.analyze_requirement(
        "请生成一份快手电商AIGC产品的介绍文档",
        CreationOptions(),
    )

    assert requirement["entity_context"]["has_high_confidence_entity"] is True
    assert requirement["entity_context"]["primary_entities"][0]["name"] == "Onepoint"
    assert generic["entity_context"]["has_high_confidence_entity"] is False


def test_entity_aware_recall_prioritizes_direct_sources_and_filters_aggregate_pages(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    _insert_entity_reference_fixture(db_path)
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)
    service.enable_vector_recall = True

    class Vector:
        def __init__(self, vector):
            self.vector = vector

    class SemanticModel:
        def encode(self, texts):
            # 所有候选均有很高语义分，验证实体层级仍能阻止泛背景反超。
            return [Vector([1.0, 0.0])] + [Vector([0.8, 0.6]) for _ in texts[1:]]

    service._embedding_model = SemanticModel()
    query = "请生成一份快手电商Onepoint产品的介绍文档"
    requirement = service.analyze_requirement(query, CreationOptions())

    references = service.retrieve_references(
        query,
        requirement,
        CreationOptions(max_references=10),
    )

    identities = [(item.source_type, item.source_id) for item in references]
    assert identities[:2] == [("knowledge", 9001), ("knowledge", 9002)]
    assert ("data", 9004) not in identities
    assert references[0].retrieval_tier == "direct"
    assert references[1].retrieval_tier == "direct"
    assert references[0].matched_entities == ("Onepoint",)
    assert "直接命中核心实体：Onepoint" in references[0].reason
    assert "召回路径" in references[0].reason
    assert all(item.retrieval_tier != "background" for item in references[:2])


def test_entity_aware_selection_limits_background_to_twenty_percent():
    from creation.service import ReferenceDocument

    def reference(source_id, tier, score):
        return ReferenceDocument(
            id=source_id,
            title=str(source_id),
            doc_type="knowledge",
            summary="",
            full_content="",
            sections_json="[]",
            style_phrases="[]",
            prompt_hint="",
            usage_count=0,
            review_status="auto_created",
            updated_at=0,
            source_url=None,
            relevance_score=score,
            quality_score=0.9,
            completeness_score=1.0,
            usage_score=0.0,
            format_score=0.55,
            freshness_score=1.0,
            final_weight=score,
            reason="",
            source_type="knowledge",
            source_id=source_id,
            retrieval_tier=tier,
        )

    ranked = [reference(1, "direct", 0.9), reference(2, "related", 0.8)]
    ranked.extend(reference(index, "background", 0.99) for index in range(3, 20))

    selected = CreationService._select_entity_aware_references(ranked, 10)

    assert [item.source_id for item in selected[:2]] == [1, 2]
    assert sum(item.retrieval_tier == "background" for item in selected) == 2


def test_origin_dedup_prefers_highest_ranked_reference_for_same_url():
    from creation.service import ReferenceDocument

    def reference(source_id, source_type, score):
        return ReferenceDocument(
            id=source_id,
            title="同一份产品资料",
            doc_type="knowledge",
            summary="Onepoint 产品介绍",
            full_content="",
            sections_json="[]",
            style_phrases="[]",
            prompt_hint="",
            usage_count=0,
            review_status="auto_created",
            updated_at=0,
            source_url="https://docs.example/onepoint?ro=false",
            relevance_score=score,
            quality_score=0.9,
            completeness_score=1.0,
            usage_score=0.0,
            format_score=0.55,
            freshness_score=1.0,
            final_weight=score,
            reason="",
            source_type=source_type,
            source_id=source_id,
        )

    selected = CreationService._deduplicate_reference_origins(
        [reference(1, "document", 0.9), reference(2, "knowledge", 0.8)]
    )

    assert [(item.source_type, item.source_id) for item in selected] == [("document", 1)]
