"""Title recall must find source-backed documents behind a compound topic."""

import hashlib
import sqlite3
from types import SimpleNamespace

from creation.service import CreationOptions, CreationService


SUBJECT = "快手大模型性能成本优化"


def _service(tmp_path):
    path = str(tmp_path / "title-candidates.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE bake_documents(
                id INTEGER PRIMARY KEY,title TEXT,doc_type TEXT,summary TEXT,
                full_content TEXT,sections_json TEXT,style_phrases TEXT,
                prompt_hint TEXT,usage_count INTEGER,review_status TEXT,
                updated_at INTEGER,source_url TEXT,deleted_at INTEGER,status TEXT);
            CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
            CREATE TABLE bake_document_source_snapshots(
                id INTEGER PRIMARY KEY,document_id INTEGER,source_url TEXT,
                page_title TEXT,content_text TEXT,identity_match INTEGER,
                completeness_status TEXT,collected_at INTEGER);
        """)
        documents = [
            (74, "大模型成本性能优化架构方案v0.1.7",
             "资源调度优化、模型推理优化与性能观测共同构成方案。",
             "https://docs.example/architecture"),
            (302, "面对成本效益挑战：大模型性能成本优化架构",
             "多平台资源需要统一规划。", "https://docs.example/partial"),
            (405, "快手大模型安全生产复盘",
             "快手大模型的安全生产措施。", "https://docs.example/safety"),
            (900, "员工活动与礼物领取指南",
             "与模型成本无关。", "https://docs.example/gifts"),
        ]
        conn.executemany("""
            INSERT INTO bake_documents VALUES (?,?,'document','',?,'[]','[]','',
                0,'auto_created',100,?,NULL,'enabled')
        """, documents)
        conn.executemany("INSERT INTO bake_document_source_snapshots VALUES (?,?,?,?,?,?,?,?)", [
            (38, 74, "https://docs.example/architecture",
             "大模型成本性能优化架构方案v0.1.7",
             "资源合池支撑统一调度；弹性伸缩应对推理流量峰谷。", 1, "complete", 200),
            (178, 302, "https://docs.example/partial",
             "面对成本效益挑战：大模型性能成本优化架构",
             "多平台资源需要统一规划。", 1, "partial", 200),
        ])
    service = CreationService.__new__(CreationService)
    service.db_path = path
    service.enable_vector_recall = False
    service._embedding_model = None
    return service


def _requirement():
    return {"keywords": [SUBJECT], "title_structure_hint": "架构", "retrieval_plan": {
        "mode": "topic", "primary_target": "快手大模", "components": [],
        "candidate_terms": ["快手大模", "模型性能"],
        "term_weights": {"快手大模": 7.2, "模型性能": 0.35},
    }}


def test_title_candidate_recalls_reordered_topic_and_preserves_original_scope(tmp_path):
    service = _service(tmp_path)
    rows = service._query_title_aligned_document_rows(
        SUBJECT, _requirement(), CreationOptions()
    )
    by_id = {row["id"]: row for row in rows if row.get("historical_source")}
    assert 74 in by_id
    assert by_id[74]["source_snapshot_id"] == 38
    assert by_id[74]["source_completeness"] == "complete"
    assert by_id[74]["full_content"].startswith("资源合池支撑统一调度")
    assert 302 not in by_id  # A partial capture is not a complete original.
    assert all(row["id"] != 900 for row in rows)


def test_title_hint_only_ranks_existing_subject_matches():
    units = CreationService._title_alignment_units(SUBJECT)
    architecture = CreationService._title_alignment_score(
        units, "大模型成本性能优化架构方案", "架构"
    )
    assert architecture > CreationService._title_alignment_score(
        units, "大模型成本性能优化周报", "架构"
    )
    assert CreationService._title_alignment_score(
        units, "不相关架构图", "架构"
    ) == 0
    assert CreationService._title_alignment_score(
        CreationService._title_alignment_units("架构图"),
        "大模型成本性能优化架构方案", "架构"
    ) == 0


def test_title_candidate_still_passes_normal_ranking_and_source_fields(tmp_path):
    service = _service(tmp_path)
    references = service.retrieve_references(
        SUBJECT, _requirement(), CreationOptions(max_references=4)
    )
    matching = next(item for item in references if item.source_id == 74)
    assert "title_alignment" in matching.retrieval_paths
    assert matching.source_snapshot_id == 38
    assert matching.refresh_completeness == "complete"
    assert matching.reason.find("标题主题匹配") >= 0
    assert all(item.source_id != 900 for item in references)


def test_structural_windows_keep_cost_performance_component_relations():
    topic = "检索模型性能成本优化"
    body = (
        "检索模型成本性能优化架构：资料正文。\n"
        "推理服务接入统一网关，由调度平台为在线请求分配算力资源，以降低调用成本。\n"
        "资源类别按业务划分。\n"
        "弹性资源池承载模型服务高峰请求，并由调度平台执行实例扩缩容，提升响应性能。\n"
        + "其他说明。\n" * 10
        + "模型服务生成报表，ROI 达到 1.5。\n"
    )
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    item = SimpleNamespace(
        title="检索模型成本性能优化架构方案",
        full_content=body,
        source_type="document",
        source_snapshot_id=27,
        source_body_hash=body_hash,
        refresh_completeness="complete",
        bridge_evidence={},
    )

    windows = CreationService.select_structural_evidence_windows(
        item, [topic, "模型", "性能", "成本"]
    )

    assert any("推理服务接入统一网关" in window["content"] for window in windows)
    assert any("弹性资源池承载模型服务" in window["content"] for window in windows)
    assert all("ROI" not in window["content"] for window in windows)
    for window in windows:
        scope = window["source_scope"]
        raw = body.encode("utf-8")[scope["start_byte"]:scope["end_byte"]]
        assert raw == window["content"].encode("utf-8")
        assert scope["excerpt_hash"] == hashlib.sha256(raw).hexdigest()
        assert scope["snapshot_id"] == 27
