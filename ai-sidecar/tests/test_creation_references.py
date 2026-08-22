from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from creation.service import CreationOptions, CreationService, ReferenceDocument


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
        "'auto_created', ?, ?, NULL, 'draft')",
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
            updated_at INTEGER, source_url TEXT, deleted_at INTEGER,
            status TEXT DEFAULT 'draft'
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
        "'auto_created', ?, ?, NULL, 'draft')",
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


def test_refined_document_keeps_representation_over_newer_capture_of_same_url(tmp_path):
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
        "'auto_created', ?, ?, NULL, 'draft')",
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

    # 烘焙文档正文已可用：同源只保留一条，且由烘焙文档代表，
    # 更新的原始采集只并入召回路径，不顶替提炼产物。
    linked = [
        item
        for item in references
        if item.source_url and "docs.example/aigc" in item.source_url
    ]
    assert len(linked) == 1
    assert linked[0].source_type == "document"
    assert linked[0].source_id == 582
    assert "keyword" in linked[0].retrieval_paths


def test_newer_capture_stands_in_while_document_content_is_still_empty(tmp_path):
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
        "'auto_created', ?, ?, NULL, 'draft')",
        (
            582,
            "AIGC 共建项目周报",
            "周报",
            "",
            "",
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

    # 提炼尚未完成（正文为空）时，较新的原始采集充当替身。
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_status", "expected_refresh_status", "expected_stat"),
    [
        ("updated", "fresh_partial", "updated"),
        ("reused", "fresh_recent_partial", "reused"),
    ],
)
async def test_document_refresh_consumes_verified_snapshot_without_replacing_baked_asset(
    monkeypatch,
    response_status,
    expected_refresh_status,
    expected_stat,
):
    calls = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json):
            calls.append((url, json))
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "status": response_status,
                    "completeness_status": "partial",
                    # document 仍代表烘焙资产；本轮创作必须只消费独立来源快照。
                    "document": {"full_content": "不可消费的烘焙正文返回值"},
                    "source_snapshot": {
                        "page_title": "即时校验标题",
                        "content_text": "本轮浏览器抓取正文",
                        "completeness_status": "partial",
                        "identity_match": True,
                        "truncated": True,
                        "collected_at": 1_787_000_000_000,
                    },
                },
            )

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    reference = ReferenceDocument(
        id=7,
        title="历史标题",
        doc_type="方案",
        summary="历史摘要",
        full_content="历史烘焙正文",
        sections_json="[]",
        style_phrases="[]",
        prompt_hint="",
        usage_count=0,
        review_status="accepted",
        updated_at=1,
        source_url="https://docs.example.com/d/home/abc123",
        relevance_score=1.0,
        quality_score=1.0,
        completeness_score=1.0,
        usage_score=0.0,
        format_score=1.0,
        freshness_score=0.1,
        final_weight=1.0,
        reason="直接命中",
        source_id=7,
    )
    service = CreationService.__new__(CreationService)

    stats = await service.refresh_recalled_documents(
        [reference],
        "请基于最新版本创作",
        require_latest=True,
    )

    assert "allow_foreground" not in calls[0][1]
    assert calls[0][1]["require_latest"] is True
    assert reference.full_content == "本轮浏览器抓取正文"
    assert reference.full_content != "不可消费的烘焙正文返回值"
    assert reference.refresh_status == expected_refresh_status
    assert reference.refresh_completeness == "partial"
    assert reference.refresh_truncated is True
    expected_stats = {
        "attempted": 1,
        "updated": 0,
        "no_change": 0,
        "reused": 0,
        "skipped": 0,
        "failed": 0,
        "complete": 0,
        "partial": 1,
    }
    expected_stats[expected_stat] = 1
    assert stats == expected_stats


@pytest.mark.asyncio
async def test_document_refresh_budget_cancels_slow_source_and_keeps_historical_content(
    monkeypatch,
):
    calls = []

    class SlowAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json):
            calls.append((url, json))
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: SlowAsyncClient(),
    )
    monkeypatch.setattr(
        "creation.service.DOCUMENT_REFRESH_TOTAL_BUDGET_SECONDS",
        0.01,
    )
    references = [
        ReferenceDocument(
            id=document_id,
            title=f"历史文档 {document_id}",
            doc_type="方案",
            summary="历史摘要",
            full_content=f"历史正文 {document_id}",
            sections_json="[]",
            style_phrases="[]",
            prompt_hint="",
            usage_count=0,
            review_status="accepted",
            updated_at=1,
            source_url=f"https://docs.example.com/d/home/{document_id}",
            relevance_score=1.0,
            quality_score=1.0,
            completeness_score=1.0,
            usage_score=0.0,
            format_score=1.0,
            freshness_score=0.1,
            final_weight=1.0,
            reason="直接命中",
            source_id=document_id,
        )
        for document_id in (7, 8)
    ]
    service = CreationService.__new__(CreationService)

    stats = await service.refresh_recalled_documents(
        references,
        "生成一份方案",
    )

    assert len(calls) == 1
    assert stats["attempted"] == 1
    assert stats["failed"] == 1
    assert [item.full_content for item in references] == ["历史正文 7", "历史正文 8"]
    assert all(item.refresh_status == "historical_only" for item in references)


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

    # 文档域：短正文不再被 3000 字分母与章节权重双重压低；无章节
    # 结构时回退为纯正文长度评分（下限 0.45）。
    document_row = {
        "source_type": "document",
        "full_content": "短正文",
        "sections_json": "[]",
        "style_phrases": "[]",
    }
    assert service._score_completeness(document_row) == 0.45
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


def test_entity_focus_text_shields_entities_from_root_background(tmp_path):
    """步骤级实体识别只看步骤焦点，根请求背景里的实体不得劫持。"""
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    _insert_entity_reference_fixture(db_path)
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    for row_id in (9101, 9102):
        conn.execute(
            "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?)",
            (
                row_id,
                f"GPU 成本治理记录 {row_id}",
                "GPU 利用率与成本数据。",
                "GPU 利用率与成本数据。",
                "GPU 利用率与成本数据。",
                '["GPU"]',
                now_ms,
                now_ms,
            ),
        )
    conn.commit()
    conn.close()
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)

    focus = "当前步骤：Onepoint 产品能力总结"
    full_query = focus + "\n整体创作背景：请生成本周 GPU 成本优化的周报"

    focused = service.analyze_requirement(
        full_query,
        CreationOptions(),
        entity_focus_text=focus,
    )
    focused_names = {
        item["name"] for item in focused["entity_context"]["primary_entities"]
    }
    assert focused_names == {"Onepoint"}

    # 不传焦点时退回全文：GPU 在语料有提炼证据且出现在文本中，仍可升级，
    # 证明隔离只作用于焦点参数而非削弱实体识别本身。
    unfocused = service.analyze_requirement(full_query, CreationOptions())
    unfocused_names = {
        item["name"] for item in unfocused["entity_context"]["primary_entities"]
    }
    assert "GPU" in unfocused_names


def test_chinese_proper_noun_is_promoted_via_corpus_expansion(tmp_path):
    """中文专名片段应能通过语料实体证据扩展成完整专名升级。

    旧实现只允许纯英文缩写成为实体候选，“AIGC共建项目”这类中英混合
    专名永远无法参选；新实现完全由语料统计证据判定。
    """
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    for row_id in (9201, 9202):
        conn.execute(
            "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?)",
            (
                row_id,
                f"AIGC 共建项目周报进展 {row_id}",
                "共建项目的推理性能优化进度。",
                "共建项目的推理性能优化进度。",
                "共建项目的推理性能优化进度。",
                '["AIGC 共建项目"]',
                now_ms,
                now_ms,
            ),
        )
    # 填充语料把总行数撑到足以计算文档频率的规模，确保“共建项目”
    # 的占比低于泛词阈值。
    for index in range(30):
        conn.execute(
            "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, '[]', 3, 0, ?, ?)",
            (
                9300 + index,
                f"无关条目 {index}",
                "其他主题记录。",
                "其他主题记录。",
                "其他主题记录。",
                now_ms,
                now_ms,
            ),
        )
    conn.commit()
    conn.close()
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)

    requirement = service.analyze_requirement(
        "总结 AIGC共建项目 的进展", CreationOptions()
    )
    entity_context = requirement["entity_context"]
    assert entity_context["has_high_confidence_entity"] is True
    names = {item["name"] for item in entity_context["primary_entities"]}
    assert "AIGC 共建项目" in names


def test_statistically_generic_terms_are_rejected_without_wordlists(tmp_path):
    """文档频率过高的泛词靠统计判别出局，不依赖词表黑名单。

    “模型”在语料里几乎每篇都出现：即使标题多次强位置提及，也不得
    升级为核心实体。
    """
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    for index in range(20):
        conn.execute(
            "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, '[]', 3, 0, ?, ?)",
            (
                9400 + index,
                f"模型运行记录 {index}",
                "模型资源与运行情况。",
                "模型资源与运行情况。",
                "模型资源与运行情况。",
                now_ms,
                now_ms,
            ),
        )
    conn.commit()
    conn.close()
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)

    requirement = service.analyze_requirement("总结 模型 和 成本", CreationOptions())
    assert requirement["entity_context"]["has_high_confidence_entity"] is False


def test_long_step_theme_phrase_is_not_promoted_to_entity(tmp_path):
    """步骤主题描述长短语不得升级为核心实体，即使语料里有提炼证据。

    复现创作记录 #85：“大模型性能成本优化周会会议纪要”被烘焙产物
    的 entities 字段自我引用出提炼证据后升级为核心实体，层级过滤
    要求候选字面包含整串，导致只有 2 条 SOP 被召回、会议纪要文档
    752 全部被剔。专名实体必须有词形上限。
    """
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    phrase = "大模型性能成本优化周会会议纪要"
    conn = sqlite3.connect(db_path)
    for row_id in (9501, 9502):
        conn.execute(
            "INSERT INTO bake_knowledge VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?)",
            (
                row_id,
                f"周会流程记录 {row_id}",
                "会议纪要整理流程。",
                "会议纪要整理流程。",
                "会议纪要整理流程。",
                '["大模型性能成本优化周会会议纪要"]',
                now_ms,
                now_ms,
            ),
        )
    conn.commit()
    conn.close()
    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)

    ctx = service._analyze_entity_context(f"当前步骤：{phrase}\n获取{phrase}")

    assert ctx["has_high_confidence_entity"] is False
    assert phrase not in ctx["candidate_entities"]
    # 短专名不受影响。
    assert CreationService._entity_length_ok("Onepoint") is True
    assert CreationService._entity_length_ok("AIGC 共建项目") is True
    assert CreationService._entity_length_ok(phrase) is False


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


def test_entity_aware_selection_orders_core_by_final_weight():
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

    # 正文强相关的 related 资料不能被标题顺带提到实体的低分 direct
    # 无条件挤出 Top-K：层级差异已由 +0.12/+0.04 加成表达。
    ranked = [reference(1, "direct", 0.72), reference(2, "related", 0.86)]

    selected = CreationService._select_entity_aware_references(ranked, 2)

    assert [item.source_id for item in selected] == [2, 1]


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


def test_origin_dedup_keeps_refined_document_when_data_snapshot_ranks_higher():
    from creation.service import ReferenceDocument

    def reference(source_id, source_type, score):
        return ReferenceDocument(
            id=source_id,
            title="大模型资源成本优化专项周会",
            doc_type="会议纪要",
            summary="会议纪要摘要",
            full_content="",
            sections_json="[]",
            style_phrases="[]",
            prompt_hint="",
            usage_count=0,
            review_status="auto_created",
            updated_at=0,
            source_url="https://docs.example/meeting/fcAC",
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

    # 数据快照因新鲜度/完备度占优排在前面，同源去重仍须保留烘焙文档。
    selected = CreationService._deduplicate_reference_origins(
        [reference(6121, "data", 0.93), reference(752, "document", 0.71)]
    )

    assert [(item.source_type, item.source_id) for item in selected] == [
        ("document", 752)
    ]


def test_refined_document_survives_same_url_data_snapshot(tmp_path):
    db_path = tmp_path / "memory-bread.db"
    _create_unified_memory_db(db_path)
    now_ms = int(time.time() * 1000)
    meeting_url = "https://docs.example/meeting/gpu-weekly"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bake_documents (id, title, doc_type, summary, full_content, "
        "sections_json, style_phrases, prompt_hint, usage_count, review_status, "
        "updated_at, source_url, deleted_at, status) "
        "VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 0, 'auto_created', ?, ?, NULL, 'enabled')",
        (
            752,
            "大模型资源成本优化专项周会纪要",
            "会议纪要",
            "会议梳理 GPU 利用率与成本优化行动项。",
            "会议梳理潮汐资源接入、GPU 利用率优化与成本优化行动项。" * 3,
            now_ms - 3_600_000,
            meeting_url,
        ),
    )
    conn.execute(
        "INSERT INTO data_sources VALUES (?, ?, ?, 'active', NULL)",
        (6121, "大模型资源成本优化专项周会", meeting_url),
    )
    conn.execute(
        "INSERT INTO data_snapshots VALUES (?, ?, ?, ?, ?, '{}', 'success', ?, ?)",
        (6122, 6121, now_ms, now_ms, "GPU利用率 0%", now_ms, now_ms),
    )
    conn.commit()
    conn.close()

    service = CreationService.__new__(CreationService)
    service.db_path = str(db_path)
    service.enable_vector_recall = False
    service._embedding_model = None

    references = service.retrieve_references(
        "本周GPU成本优化的周会会议纪要",
        {"keywords": ["GPU", "成本优化"]},
        CreationOptions(max_references=8),
    )

    origins = {(item.source_type, item.source_id) for item in references}
    assert ("document", 752) in origins
    assert ("data", 6121) not in origins


def test_extract_keywords_drops_sliding_ngrams_and_contained_tokens():
    service = CreationService.__new__(CreationService)
    query = (
        "当前步骤：大模型性能成本优化周会会议纪要\n"
        "用@记忆搜索 Tool 获取本周大模型性能成本优化周会会议纪要，"
        "并总结为列表展示，最多5行文字，尽可能体现数字化的结果指标"
    )
    keywords = service._extract_keywords(query)

    assert len(keywords) <= 16
    # 不再生成滑窗 n-gram 噪声词。
    for noise in ("大模型性能成", "模型性能成本", "型性能成本优", "周会会议"):
        assert noise not in keywords
    # 完整长短语保留；父短语已在时，4 字前缀后备词不重复进入分母。
    assert "大模型性能成本优化周会会议纪要" in keywords
    assert "大模型性" not in keywords
    # 输出格式指令是执行包装，不能作为主题词进入分母。
    assert "结果指标" not in keywords
    assert "列表展示" not in keywords
    assert not any("尽可能" in keyword for keyword in keywords)


def test_meeting_note_lexical_score_not_diluted_by_noise_keywords():
    # 复现创作记录 #83：步骤主题词被滑窗 n-gram 稀释后，16 个关键词里
    # 会议纪要文档只命中 2 个，lexical 被压到 0.4 阈值边缘。治理后
    # 词表收敛为独立主题词，同一文档应给出更高的相关性得分。
    service = CreationService.__new__(CreationService)
    keywords = service._extract_keywords(
        "当前步骤：大模型性能成本优化周会会议纪要\n"
        "用@记忆搜索 Tool 获取本周大模型性能成本优化周会会议纪要，并总结为列表展示，"
        "最多5行文字，尽可能体现数字化的结果指标\n"
        "整体创作背景：请生成下本周GPU成本优化的周报"
    )
    assert len(keywords) <= 8
    row = {
        "title": "会议录制: 大模型资源成本优化专项周会 - 云文档",
        "doc_type": "会议纪要",
        "summary": "会议梳理潮汐资源接入与 GPU 利用率优化。",
        "full_content": "会议讨论了 GPU 利用率与成本优化行动项。" * 5,
        "sections_json": "[]",
        "prompt_hint": "",
    }
    diagnostics = service._relevance_diagnostics(
        row, {"keywords": keywords}
    )
    assert diagnostics["lexical_score"] >= 0.45
    assert "成本优化" in diagnostics["matched_keywords"]
    assert "GPU" in diagnostics["matched_keywords"]


def test_short_complete_document_not_penalized_by_long_content_denominator():
    # 会议纪要等短而完整的文档（约 1600 字正文）不应被 3000 字分母
    # 压低完备度；与提炼型记忆使用同一饱和长度。
    service = CreationService.__new__(CreationService)
    row = {
        "source_type": "document",
        "full_content": "议" * 1600,
        "sections_json": "[]",
    }

    assert service._score_completeness(row) >= 0.65
