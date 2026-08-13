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


def test_chinese_keyword_fallback_and_doc_type_cover_gift_guide():
    service = CreationService.__new__(CreationService)

    keywords = service._extract_keywords("写一份周年员工的礼物指南")

    assert any("周年" in keyword for keyword in keywords)
    assert any("礼物" in keyword for keyword in keywords)
    assert service._infer_doc_type("写一份周年员工的礼物指南") == "指南"


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
