"""timeline 3194 混组问题的前置分组与落库前一致性门禁回归测试。"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import knowledge.extractor_v2 as extractor_module
from background_processor import BackgroundProcessor
from knowledge.extractor_v2 import (
    KnowledgeExtractorV2,
    _validated_capture_groups,
    _validated_coherent_capture_group,
)
from knowledge.fragment_grouper import FragmentGrouper


class _MappedEmbeddingModel:
    def __init__(self, mappings):
        self.mappings = mappings

    def encode(self, texts):
        encoded = []
        for text in texts:
            vector = next(
                vector for marker, vector in self.mappings if marker in text
            )
            encoded.append(SimpleNamespace(vector=vector))
        return encoded


def _capture(capture_id, app_name, text, url=None):
    return {
        "id": capture_id,
        "ts": capture_id * 1000,
        "app_name": app_name,
        "window_title": app_name,
        "ocr_text": text,
        "ax_text": "",
        "input_text": "",
        "audio_text": "",
        "url": url,
    }


def test_surface_switch_guard_splits_timeline_3194_shape():
    """组主题向量再相似，也不能吞掉不同应用/页面的无关内容。"""
    model = _MappedEmbeddingModel([
        ("时间线修复", [1.0, 0.0, 0.0, 0.0]),
        ("杭州天气", [0.6, 0.8, 0.0, 0.0]),
        ("Kim消息", [0.0, 0.6, 0.8, 0.0]),
        ("GPU用量", [0.0, 0.0, 0.6, 0.8]),
    ])
    captures = [
        _capture(1, "ChatGPT", "时间线修复 今天 已过滤"),
        _capture(2, "Google Chrome", "杭州天气 今天 已过滤", "https://weather.example/hangzhou"),
        _capture(3, "Kim", "Kim消息 今天 已过滤"),
        _capture(4, "Google Chrome", "GPU用量 项目 成本", "https://gpu.example/projects"),
    ]

    groups = FragmentGrouper(model).group_captures(captures)

    assert [[capture["id"] for capture in group] for group in groups] == [
        [1], [2], [3], [4]
    ]


def test_surface_switch_guard_keeps_near_duplicate_cross_app_flow():
    """跨工具正文近乎重复时仍允许保持同组，避免把应用切换当绝对边界。"""
    model = _MappedEmbeddingModel([
        ("extractor_v2", [1.0, 0.0]),
    ])
    captures = [
        _capture(1, "ChatGPT", "MemoryBread extractor_v2 一致性门禁实现"),
        _capture(2, "Visual Studio Code", "MemoryBread extractor_v2 一致性门禁实现"),
    ]

    groups = FragmentGrouper(model).group_captures(captures)

    assert [[capture["id"] for capture in group] for group in groups] == [[1, 2]]


def test_validated_capture_groups_requires_exact_partition():
    assert _validated_capture_groups(
        [[1, 2, 3, 4]],
        [1, 2, 3, 4],
        minimum_groups=1,
    ) == [[1, 2, 3, 4]]
    assert _validated_capture_groups(
        [1, "2", 3, 4],
        [1, 2, 3, 4],
        minimum_groups=1,
    ) == [[1, 2, 3, 4]]
    assert _validated_capture_groups(
        [1, 2, 3, 4],
        [1, 2, 3, 4],
    ) == []
    assert _validated_capture_groups([["3", 1], [4, 2]], [1, 2, 3, 4]) == [
        [1, 3], [2, 4]
    ]
    assert _validated_capture_groups([[1, 2], [2, 3, 4]], [1, 2, 3, 4]) == []
    assert _validated_capture_groups([[1, 2], [3]], [1, 2, 3, 4]) == []
    assert _validated_capture_groups([[1, 2], [3, 99]], [1, 2, 3, 4]) == []


def test_coherent_capture_group_expands_only_a_valid_flat_representative_subset():
    assert _validated_coherent_capture_group(
        [1, "3"],
        [1, 2, 3, 4],
    ) == [[1, 2, 3, 4]]
    assert _validated_coherent_capture_group(
        [[1, 3]],
        [1, 2, 3, 4],
    ) == []
    assert _validated_coherent_capture_group([1, 99], [1, 2, 3, 4]) == []
    assert _validated_coherent_capture_group([1, 1], [1, 2, 3, 4]) == []


class _NoopTracker:
    def __init__(self, *args, **kwargs):
        self._prompt_tokens = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_response(self, response):
        return None

    def set_tokens(self, prompt, completion):
        return None


def test_extract_merged_returns_split_signal_before_persistence(monkeypatch):
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "mock-model"
    extractor.user_identity = ""
    extractor._generate_segments = lambda captures: ([], [], [])
    extractor._ollama_chat = lambda **kwargs: {
        "message": {
            "content": json.dumps({
                "is_coherent": False,
                "coherence_reason": "天气查询与代码修复无关",
                "capture_groups": [[1, 3], [2, 4]],
            }, ensure_ascii=False)
        }
    }
    monkeypatch.setattr(extractor_module, "_rag_is_active", lambda: False)
    monkeypatch.setattr("monitor.llm_tracker.LLMCallTracker", _NoopTracker)

    captures = [
        _capture(1, "ChatGPT", "时间线修复正文"),
        _capture(2, "Chrome", "杭州天气正文"),
        _capture(3, "ChatGPT", "时间线修复测试"),
        _capture(4, "Chrome", "杭州天气预报"),
    ]
    result = extractor.extract_merged(captures)

    assert result == {
        "_split_required": True,
        "capture_groups": [[1, 3], [2, 4]],
        "split_reason": "天气查询与代码修复无关",
    }


def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL,
            app_name TEXT,
            win_title TEXT,
            ocr_text TEXT,
            ax_text TEXT,
            input_text TEXT,
            audio_text TEXT,
            timeline_id INTEGER,
            url TEXT,
            webpage_title TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id INTEGER NOT NULL,
            summary TEXT,
            overview TEXT,
            details TEXT,
            entities TEXT,
            category TEXT,
            importance INTEGER,
            occurrence_count INTEGER,
            capture_ids TEXT,
            start_time INTEGER,
            end_time INTEGER,
            duration_minutes INTEGER,
            frag_app_name TEXT,
            frag_win_title TEXT,
            time_range_start INTEGER,
            time_range_end INTEGER,
            key_timestamps TEXT,
            observed_at INTEGER,
            event_time_start INTEGER,
            event_time_end INTEGER,
            history_view INTEGER NOT NULL DEFAULT 0,
            content_origin TEXT,
            activity_type TEXT,
            is_self_generated INTEGER NOT NULL DEFAULT 0,
            evidence_strength TEXT,
            created_at_ms INTEGER,
            updated_at_ms INTEGER
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO captures (
            id, ts, app_name, win_title, ocr_text, ax_text,
            input_text, audio_text, timeline_id
        ) VALUES (?, ?, ?, ?, ?, '', '', '', NULL)
        """,
        [
            (1, 1000, "ChatGPT", "ChatGPT", "代码任务第一帧"),
            (2, 2000, "Chrome", "天气", "天气任务第一帧"),
            (3, 3000, "ChatGPT", "ChatGPT", "代码任务第二帧"),
            (4, 4000, "Chrome", "天气", "天气任务第二帧"),
        ],
    )
    conn.commit()
    conn.close()


class _ImmediateQueue:
    def submit_sync(self, _priority, fn, timeout=None, lane=None):
        return fn()


class _SplitThenExtract:
    def extract_merged(self, captures, preempt_check=None):
        capture_ids = [capture["id"] for capture in captures]
        if capture_ids == [1, 2, 3, 4]:
            return {
                "_split_required": True,
                "capture_groups": [[1, 3], [2, 4]],
                "split_reason": "两个独立任务",
            }
        label = "代码任务" if capture_ids == [1, 3] else "天气任务"
        return {
            "capture_ids": json.dumps(capture_ids),
            "summary": label,
            "overview": label + "的连续记录",
            "details": label + "详情",
            "entities": "[]",
            "category": "代码" if label == "代码任务" else "其他",
            "importance": 3,
            "occurrence_count": 1,
            "start_time": captures[0]["ts"],
            "end_time": captures[-1]["ts"],
            "duration_minutes": 0,
            "time_range_start": captures[0]["ts"],
            "time_range_end": captures[-1]["ts"],
            "key_timestamps": "[]",
            "frag_app_name": captures[-1]["app_name"],
            "frag_win_title": captures[-1]["window_title"],
            "observed_at": captures[-1]["ts"],
            "is_self_generated": False,
        }

    def _find_similar_knowledge(self, overview, db_conn, **kwargs):
        return None


class _FailGroupThenExtractSingles(_SplitThenExtract):
    def extract_merged(self, captures, preempt_check=None):
        if len(captures) > 1:
            return None
        return super().extract_merged(captures, preempt_check=preempt_check)


async def _skip_vectorization(*args, **kwargs):
    return True


def test_background_processor_persists_split_groups_separately(tmp_path, monkeypatch):
    db_path = str(tmp_path / "coherence.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SplitThenExtract())
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    captures = [
        _capture(1, "ChatGPT", "代码任务第一帧"),
        _capture(2, "Chrome", "天气任务第一帧"),
        _capture(3, "ChatGPT", "代码任务第二帧"),
        _capture(4, "Chrome", "天气任务第二帧"),
    ]
    assert asyncio.run(processor._process_capture_group(captures)) is True

    conn = sqlite3.connect(db_path)
    timelines = conn.execute(
        "SELECT summary, capture_ids FROM timelines ORDER BY id"
    ).fetchall()
    capture_links = conn.execute(
        "SELECT id, timeline_id FROM captures ORDER BY id"
    ).fetchall()
    conn.close()

    assert [(summary, json.loads(ids)) for summary, ids in timelines] == [
        ("代码任务", [1, 3]),
        ("天气任务", [2, 4]),
    ]
    assert capture_links[0][1] == capture_links[2][1]
    assert capture_links[1][1] == capture_links[3][1]
    assert capture_links[0][1] != capture_links[1][1]


def test_repeated_group_failure_falls_back_to_single_captures(tmp_path, monkeypatch):
    db_path = str(tmp_path / "coherence.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(
        processor,
        "_get_knowledge_extractor",
        lambda: _FailGroupThenExtractSingles(),
    )
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())
    captures = [
        _capture(1, "ChatGPT", "代码任务第一帧"),
        _capture(2, "Chrome", "天气任务第一帧"),
        _capture(3, "ChatGPT", "代码任务第二帧"),
        _capture(4, "Chrome", "天气任务第二帧"),
    ]

    assert asyncio.run(processor._process_capture_group(captures)) is False
    assert asyncio.run(processor._process_capture_group(captures)) is True

    conn = sqlite3.connect(db_path)
    capture_links = conn.execute(
        "SELECT id, timeline_id FROM captures ORDER BY id"
    ).fetchall()
    conn.close()
    assert all(timeline_id is not None for _, timeline_id in capture_links)
    assert processor._timeline_retry_state == {}


def test_merged_prompt_exposes_capture_ids():
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    merged = extractor._build_merged_blocks([
        _capture(3194, "ChatGPT", "时间线一致性排查正文"),
    ])

    assert "采集ID:3194" in merged


def test_merged_prompt_preserves_every_id_when_bodies_are_identical():
    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    captures = [
        _capture(69803, "Chrome", "完全相同的页面正文"),
        _capture(69832, "Chrome", "完全相同的页面正文"),
        _capture(69846, "Chrome", "完全相同的页面正文"),
        _capture(69847, "Chrome", "完全相同的页面正文"),
    ]

    merged = extractor._build_merged_blocks(captures)

    for capture in captures:
        assert f"采集ID:{capture['id']}" in merged
    assert merged.count("完全相同的页面正文") == 1
