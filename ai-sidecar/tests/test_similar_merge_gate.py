"""相似度去重合并门（基础阈值 + 实体一致性）的回归测试。

复现 timeline 2713 脏数据场景：两个措辞相近但主题不同的排查任务
（"排查 ID1230 低价值数据" vs "排查时间线 2148"）余弦相似度约 0.76，
旧阈值 0.72 导致整段 21 条无关采集被误并入已有时间线。
"""

import math
import json
import sqlite3
from typing import Dict, List

from knowledge.extractor_v2 import KnowledgeExtractorV2, discarded_knowledge


class _Emb:
    def __init__(self, vector: List[float]):
        self.vector = vector


class _FakeEmbeddingModel:
    """按文本查表的假向量模型，用于精确控制余弦相似度。"""

    def __init__(self, mapping: Dict[str, List[float]]):
        self.mapping = mapping

    def encode(self, texts: List[str]):
        return [_Emb(self.mapping[text]) for text in texts]


def _vector_with_cosine(cosine: float) -> List[float]:
    return [cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine))]


def _init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id INTEGER,
            overview TEXT,
            entities TEXT,
            occurrence_count INTEGER,
            start_time INTEGER,
            end_time INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


# 新片段时间窗与已有条目相隔 3 小时，避开"连续片段保护"（15 分钟）
_NEW_START = 10_000_000_000
_NEW_END = 10_000_600_000
_EXISTING_START = _NEW_START - 3 * 3600 * 1000
_EXISTING_END = _EXISTING_START + 600_000


def _make_extractor(db_conn, existing_overview: str, cosine: float, existing_entities: str):
    db_conn.execute(
        """
        INSERT INTO timelines (
            capture_id, overview, entities, occurrence_count, start_time, end_time
        ) VALUES (1, ?, ?, 1, ?, ?)
        """,
        (existing_overview, existing_entities, _EXISTING_START, _EXISTING_END),
    )
    db_conn.commit()

    model = _FakeEmbeddingModel({
        "新任务概述": [1.0, 0.0],
        existing_overview: _vector_with_cosine(cosine),
    })
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.embedding_model = model
    return extractor


def _find(extractor, db_conn, entities):
    return extractor._find_similar_knowledge(
        "新任务概述",
        db_conn,
        entities=entities,
        start_time=_NEW_START,
        end_time=_NEW_END,
    )


def test_merge_rejected_below_threshold_even_with_entity_overlap(tmp_path) -> None:
    """0.76 < 0.80：即使共享实体（如 MemoryBread）也不得合并 —— 2713 脏数据的直接根因。"""
    db_path = str(tmp_path / "gate.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    extractor = _make_extractor(
        conn, "排查 ID1230 低价值数据", 0.76, '["MemoryBread", "ID1230"]'
    )
    assert _find(extractor, conn, ["MemoryBread", "2148"]) is None
    conn.close()


def test_merge_accepted_above_threshold_with_entity_overlap(tmp_path) -> None:
    """0.82 >= 0.80 且有实体交集：正常合并，守卫不误伤。"""
    db_path = str(tmp_path / "gate.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    extractor = _make_extractor(
        conn, "排查 ID1230 低价值数据", 0.82, '["MemoryBread", "ID1230"]'
    )
    assert _find(extractor, conn, ["MemoryBread", "ID1230"]) == 1
    conn.close()


def test_merge_rejected_when_entities_disjoint(tmp_path) -> None:
    """0.82 过阈但新旧实体零交集：视为同项目不同任务，拒绝合并。"""
    db_path = str(tmp_path / "gate.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    extractor = _make_extractor(
        conn, "排查 ID1230 低价值数据", 0.82, '["MemoryBread", "ID1230"]'
    )
    assert _find(extractor, conn, ["万擎", "SLO"]) is None
    conn.close()


def test_merge_accepted_for_near_duplicate_without_entity_overlap(tmp_path) -> None:
    """基础相似度 >= 0.86 视为近乎重复，即使实体不重合也允许合并。"""
    db_path = str(tmp_path / "gate.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    extractor = _make_extractor(
        conn, "排查 ID1230 低价值数据", 0.87, '["MemoryBread", "ID1230"]'
    )
    assert _find(extractor, conn, ["万擎", "SLO"]) == 1
    conn.close()


def test_merge_falls_back_to_similarity_when_new_entities_missing(tmp_path) -> None:
    """新知识无实体时退化为纯相似度判断，过阈即合并。"""
    db_path = str(tmp_path / "gate.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    extractor = _make_extractor(
        conn, "排查 ID1230 低价值数据", 0.82, '["MemoryBread", "ID1230"]'
    )
    assert _find(extractor, conn, []) == 1
    conn.close()


def test_generate_segments_filters_discarded_segments(monkeypatch) -> None:
    """分段提炼确定性丢弃时，该分段 capture 不进 segments 而是单独透传。

    防止低价值 capture（如系统工具菜单截图）与真实工作 capture 同组时
    被一起写入时间线成员（timeline 2713 类污染的另一条路径）。
    """
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)

    def fake_extract_sync(capture_data, db_conn=None):
        if capture_data.get('id') == 101:
            return discarded_knowledge('no_value')
        return {'summary': '有效工作内容'}

    monkeypatch.setattr(extractor, 'extract_sync', fake_extract_sync)

    captures = [
        {'id': 101, 'ts': 1000, 'app_name': 'Amphetamine',
         'window_title': 'Menu', 'ocr_text': '开启新会话…'},
        {'id': 102, 'ts': 2000, 'app_name': 'Qoder',
         'window_title': 'Quest', 'ocr_text': '真实工作内容'},
    ]
    segments, discarded_ids, _ = extractor._generate_segments(captures)

    assert discarded_ids == [101]
    assert [s['capture_ids'] for s in segments] == [[102]]


def test_generate_segments_keeps_all_when_no_discard(monkeypatch) -> None:
    """无丢弃分段时行为不变，discarded 列表为空。"""
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)

    def fake_extract_sync(capture_data, db_conn=None):
        return {'summary': '有效工作内容'}

    monkeypatch.setattr(extractor, 'extract_sync', fake_extract_sync)

    captures = [
        {'id': 201, 'ts': 1000, 'app_name': 'Qoder',
         'window_title': 'Quest', 'ocr_text': '内容 A'},
        {'id': 202, 'ts': 2000, 'app_name': 'Qoder',
         'window_title': 'Quest', 'ocr_text': '内容 B'},
    ]
    segments, discarded_ids, _ = extractor._generate_segments(captures)

    assert discarded_ids == []
    assert [s['capture_ids'] for s in segments] == [[201, 202]]


def test_extract_merged_single_valid_capture_no_unbound_error(monkeypatch) -> None:
    """真实 extract_merged 单条路径回归：有效产出时不得报 UnboundLocalError。

    线上曾因单条分支残留对未定义变量 discarded_capture_ids 的引用，
    导致所有单 capture 提炼批次报 "cannot access local variable" 异常。
    """
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    monkeypatch.setattr(
        extractor,
        'extract_sync',
        lambda capture_data, db_conn=None: {'summary': '有效工作', 'overview': '完成了有效工作'},
    )

    result = extractor.extract_merged(
        [{'id': 301, 'ts': 1000, 'app_name': 'Qoder', 'window_title': 'Quest'}]
    )

    assert result is not None
    assert not result.get('_discarded')
    assert json.loads(result['capture_ids']) == [301]


def test_extract_merged_single_discarded_capture_passthrough(monkeypatch) -> None:
    """单条路径确定性丢弃标记透传，由调用方消费。"""
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    monkeypatch.setattr(
        extractor,
        'extract_sync',
        lambda capture_data, db_conn=None: discarded_knowledge('no_value'),
    )

    result = extractor.extract_merged(
        [{'id': 302, 'ts': 1000, 'app_name': 'Amphetamine', 'window_title': 'Menu'}]
    )

    assert result is not None
    assert result.get('_discarded') is True
    assert result.get('discard_reason') == 'no_value'
