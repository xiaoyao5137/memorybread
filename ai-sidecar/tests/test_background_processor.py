import asyncio
import json
import sqlite3
import time

from background_processor import (
    BackgroundProcessor,
    _is_self_generated_capture,
    _TIMELINE_MAX_MEMBER_COUNT,
    _TIMELINE_MAX_OCCURRENCE_COUNT,
    _TIMELINE_MAX_SPAN_HOURS,
)
from knowledge.fragment_grouper import FragmentGrouper


class _StubVectorStorage:
    def __init__(self) -> None:
        self.calls = []

    def store_vector(self, capture_id, text, vector, metadata=None):
        self.calls.append({
            "capture_id": capture_id,
            "text": text,
            "vector": vector,
            "metadata": metadata or {},
        })
        return True


def _init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
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
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.commit()
    conn.close()


def test_pending_capture_query_includes_input_and_audio_text(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO captures (
            id, ts, app_name, win_title, ocr_text, ax_text,
            input_text, audio_text, timeline_id
        )
        VALUES (?, ?, 'Code', '工作窗口', ?, ?, ?, ?, NULL)
        """,
        [
            (1, 1000, "", "", "用户输入的需求", ""),
            (2, 2000, "", "", "", "会议音频转写"),
            (3, 3000, "OCR 正文", "", "", ""),
        ],
    )
    conn.commit()

    processor = BackgroundProcessor(db_path=db_path)
    captures = processor._get_unprocessed_captures(conn, limit=10)
    conn.close()

    assert processor._count_unprocessed_captures() == 3
    assert [capture["id"] for capture in captures] == [1, 2, 3]
    assert captures[0]["input_text"] == "用户输入的需求"
    assert captures[1]["audio_text"] == "会议音频转写"


def test_fragment_grouper_uses_input_and_audio_text() -> None:
    grouper = FragmentGrouper()

    assert grouper._capture_text({"input_text": "用户输入"}) == "用户输入"
    assert grouper._capture_text({"audio_text": "会议转写"}) == "会议转写"
    assert grouper._get_semantic_text({"input_text": "这是超过十个字符的用户输入需求正文"})


def test_charging_catchup_requires_progress(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    profile = type(
        "_Profile",
        (),
        {"mode": "charging", "timeline_batch_size": 20},
    )()

    assert processor._should_continue_charging_catchup(
        profile, 122, 34, {"processed_count": 1}
    ) is True
    assert processor._should_continue_charging_catchup(
        profile, 34, 34, {"processed_count": 0, "reason": "idle_not_enough"}
    ) is False


def test_is_self_generated_capture_matches_memory_bread() -> None:
    assert _is_self_generated_capture("memory-bread-desktop", "问答页") is True
    assert _is_self_generated_capture("其他应用", "记忆面包 RagPanel") is True
    assert _is_self_generated_capture("Google Chrome", "Claude") is False


def test_fragment_grouper_splits_history_review_from_live_chat() -> None:
    grouper = FragmentGrouper()
    captures = [
        {
            "id": 1,
            "ts": 1000,
            "app_name": "WeChat",
            "window_title": "聊天窗口",
            "ax_text": "今天和产品同步需求，正在回复最新消息",
            "ocr_text": None,
        },
        {
            "id": 2,
            "ts": 2000,
            "app_name": "WeChat",
            "window_title": "聊天记录",
            "ax_text": "回看昨天的聊天记录，查看前天的历史消息",
            "ocr_text": None,
        },
    ]

    assert grouper._history_mode_changed([captures[0]], captures[1]) is True
    assert grouper._check_context_continuity([captures[0]], captures[1]) is False


def test_fragment_grouper_merges_same_document_url() -> None:
    grouper = FragmentGrouper()
    doc_url = "https://docs.corp.kuaishou.com/d/home/fcAAAAAA"
    captures = [
        {
            "id": 1,
            "ts": 1000,
            "app_name": "Google Chrome",
            "window_title": "方案 A - 云文档",
            "ax_text": "方案 A 的完整正文内容，用于验证同一文档连续浏览。",
            "ocr_text": None,
            "url": doc_url,
        },
        {
            "id": 2,
            "ts": 2000,
            "app_name": "Google Chrome",
            "window_title": "方案 A - 云文档",
            "ax_text": "方案 A 的完整正文内容，用于验证同一文档连续浏览。",
            "ocr_text": None,
            "url": f"{doc_url}#section=details",
        },
    ]

    groups = grouper.group_captures(captures)

    assert [[capture["id"] for capture in group] for group in groups] == [[1, 2]]


def test_fragment_grouper_splits_different_or_empty_document_url() -> None:
    grouper = FragmentGrouper()
    captures = [
        {
            "id": 1,
            "ts": 1000,
            "app_name": "Google Chrome",
            "window_title": "方案 A - 云文档",
            "ax_text": "方案正文内容",
            "ocr_text": None,
            "url": "https://docs.corp.kuaishou.com/d/home/fcAAAAAA",
        },
        {
            "id": 2,
            "ts": 2000,
            "app_name": "Google Chrome",
            "window_title": "方案 B - 云文档",
            "ax_text": "方案正文内容",
            "ocr_text": None,
            "url": "https://docs.corp.kuaishou.com/d/home/fcBBBBBB",
        },
        {
            "id": 3,
            "ts": 3000,
            "app_name": "ChatGPT Atlas",
            "window_title": "方案 B - 云文档",
            "ax_text": "方案正文内容",
            "ocr_text": None,
            "url": None,
        },
        {
            "id": 4,
            "ts": 4000,
            "app_name": "ChatGPT Atlas",
            "window_title": "方案 B - 云文档",
            "ax_text": "方案正文内容",
            "ocr_text": None,
            "url": None,
        },
    ]

    groups = grouper.group_captures(captures)

    assert [[capture["id"] for capture in group] for group in groups] == [
        [1],
        [2],
        [3],
        [4],
    ]


def test_save_knowledge_persists_semantic_fields(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    conn = sqlite3.connect(db_path)

    knowledge = {
        "capture_ids": "[1,2]",
        "overview": "今天回看了昨天的飞书消息",
        "details": "确认了昨天讨论的发布安排",
        "entities": "[\"飞书\", \"发布\"]",
        "category": "聊天",
        "importance": 4,
        "occurrence_count": 1,
        "start_time": 1000,
        "end_time": 2000,
        "duration_minutes": 1,
        "frag_app_name": "Feishu",
        "frag_win_title": "项目群",
        "observed_at": 2000,
        "event_time_start": 500,
        "event_time_end": 800,
        "history_view": True,
        "content_origin": "historical_content",
        "activity_type": "reviewing_history",
        "is_self_generated": False,
        "evidence_strength": "high",
    }

    knowledge_id = processor._save_knowledge(conn, knowledge)
    row = conn.execute(
        "SELECT observed_at, event_time_start, event_time_end, history_view, content_origin, activity_type, is_self_generated, evidence_strength FROM timelines WHERE id = ?",
        (knowledge_id,),
    ).fetchone()
    conn.close()

    assert row == (2000, 500, 800, 1, "historical_content", "reviewing_history", 0, "high")


class _ImmediateQueue:
    def submit_sync(self, _priority, fn, timeout=None, lane=None):
        return fn()


class _SimilarExtractor:
    def __init__(self, similar_id: int) -> None:
        self.similar_id = similar_id
        self.extract_calls = 0

    def extract_merged(self, captures, preempt_check=None):
        self.extract_calls += 1
        capture_ids = [capture["id"] for capture in captures]
        return {
            "capture_ids": json.dumps(capture_ids),
            "summary": "万擎平台稳定性设计",
            "overview": "整理万擎平台稳定性设计与调度策略",
            "details": "补充新的文档内容",
            "entities": json.dumps(["万擎", "SLO"]),
            "category": "文档",
            "importance": 4,
            "occurrence_count": 1,
            "start_time": captures[0]["ts"],
            "end_time": captures[-1]["ts"],
            "duration_minutes": 0,
            "time_range_start": captures[0]["ts"],
            "time_range_end": captures[-1]["ts"],
            "key_timestamps": json.dumps([]),
            "frag_app_name": captures[-1].get("app_name"),
            "frag_win_title": captures[-1].get("window_title"),
            "observed_at": captures[-1]["ts"],
            "content_origin": "document_reference",
            "activity_type": "reading",
            "is_self_generated": False,
            "evidence_strength": "high",
        }

    def _find_similar_knowledge(self, overview, db_conn, **kwargs):
        return self.similar_id


def _seed_timeline(conn: sqlite3.Connection, doc_url: str | None) -> int:
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (1, 1000, 'Chrome', 'Doc A', 'doc a', '', 1, ?, 'Doc A')
        """,
        (doc_url,),
    )
    conn.execute(
        """
        INSERT INTO timelines (
            id, capture_id, summary, overview, details, entities, category, importance,
            occurrence_count, capture_ids, start_time, end_time, time_range_start,
            time_range_end, observed_at, content_origin, activity_type, evidence_strength,
            created_at_ms, updated_at_ms
        )
        VALUES (1, 1, 'Doc A', '万擎平台稳定性设计', '已有内容', '[]', '文档', 4,
                1, '[1]', 1000, 1000, 1000, 1000, 1000, 'document_reference',
                'reading', 'high', 1000, 1000)
        """
    )
    conn.commit()
    return 1


async def _skip_vectorization(*_args, **_kwargs):
    return True


def test_similar_merge_rejects_different_document_url(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    doc_a = "https://docs.corp.kuaishou.com/k/home/docA/fcAAAAAA"
    doc_b = "https://docs.corp.kuaishou.com/k/home/docB/fcBBBBBB"
    conn = sqlite3.connect(db_path)
    _seed_timeline(conn, doc_a)
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (2, 2000, 'Chrome', 'Doc B', 'doc b', '', NULL, ?, 'Doc B')
        """,
        (doc_b,),
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SimilarExtractor(1))
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    ok = asyncio.run(processor._process_capture_group([
        {
            "id": 2,
            "ts": 2000,
            "app_name": "Chrome",
            "window_title": "Doc B",
            "ocr_text": "doc b",
            "ax_text": "",
            "url": doc_b,
        }
    ]))

    conn = sqlite3.connect(db_path)
    linked_timeline = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    original_capture_ids = conn.execute("SELECT capture_ids FROM timelines WHERE id = 1").fetchone()[0]
    conn.close()

    assert ok is True
    assert linked_timeline != 1
    assert timeline_count == 2
    assert json.loads(original_capture_ids) == [1]


def test_similar_merge_allows_same_document_and_syncs_capture_ids(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    doc_a = "https://docs.corp.kuaishou.com/k/home/docA/fcAAAAAA"
    conn = sqlite3.connect(db_path)
    _seed_timeline(conn, doc_a)
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (2, 2000, 'Chrome', 'Doc A', 'doc a part 2', '', NULL, ?, 'Doc A')
        """,
        (doc_a,),
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SimilarExtractor(1))
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    ok = asyncio.run(processor._process_capture_group([
        {
            "id": 2,
            "ts": 2000,
            "app_name": "Chrome",
            "window_title": "Doc A",
            "ocr_text": "doc a part 2",
            "ax_text": "",
            "url": doc_a,
        }
    ]))

    conn = sqlite3.connect(db_path)
    linked_timeline = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    capture_ids, end_time = conn.execute(
        "SELECT capture_ids, end_time FROM timelines WHERE id = 1"
    ).fetchone()
    conn.close()

    assert ok is True
    assert linked_timeline == 1
    assert json.loads(capture_ids) == [1, 2]
    assert end_time == 2000


def test_forced_cross_batch_merge_still_runs_model_extraction(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    doc_a = "https://docs.corp.kuaishou.com/k/home/docA/fcAAAAAA"
    conn = sqlite3.connect(db_path)
    _seed_timeline(conn, doc_a)
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (2, 2000, 'Chrome', 'Doc A', '包含6.28万成本节省的新片段', '', NULL, ?, 'Doc A')
        """,
        (doc_a,),
    )
    conn.commit()
    conn.close()

    extractor = _SimilarExtractor(999)
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: extractor)
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    ok = asyncio.run(processor._process_capture_group(
        [{
            "id": 2,
            "ts": 2000,
            "app_name": "Chrome",
            "window_title": "Doc A",
            "ocr_text": "包含6.28万成本节省的新片段",
            "ax_text": "",
            "url": doc_a,
        }],
        merge_timeline_id=1,
    ))

    conn = sqlite3.connect(db_path)
    linked_timeline = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    conn.close()

    assert ok is True
    assert extractor.extract_calls == 1
    assert linked_timeline == 1


def test_similar_merge_rejects_empty_document_url(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    doc_a = "https://docs.corp.kuaishou.com/k/home/docA/fcAAAAAA"
    conn = sqlite3.connect(db_path)
    _seed_timeline(conn, doc_a)
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (2, 2000, 'ChatGPT Atlas', 'Doc A - 云文档', 'doc a part 2', '', NULL, NULL, 'Doc A')
        """
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SimilarExtractor(1))
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    ok = asyncio.run(processor._process_capture_group([
        {
            "id": 2,
            "ts": 2000,
            "app_name": "ChatGPT Atlas",
            "window_title": "Doc A - 云文档",
            "ocr_text": "doc a part 2",
            "ax_text": "",
            "url": None,
        }
    ]))

    conn = sqlite3.connect(db_path)
    linked_timeline = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    original_capture_ids = conn.execute("SELECT capture_ids FROM timelines WHERE id = 1").fetchone()[0]
    conn.close()

    assert ok is True
    assert linked_timeline != 1
    assert timeline_count == 2
    assert json.loads(original_capture_ids) == [1]


def test_empty_url_document_timeline_rejects_cross_batch_merge(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    _seed_timeline(conn, None)
    conn.execute(
        """
        UPDATE captures SET win_title = '方案 A - 云文档' WHERE id = 1
        """
    )
    conn.execute(
        """
        INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, timeline_id, url, webpage_title)
        VALUES (2, 2000, 'ChatGPT Atlas', '方案 A - 云文档', 'doc a part 2', '', NULL, NULL, '方案 A - 云文档')
        """
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SimilarExtractor(1))
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    ok = asyncio.run(processor._process_capture_group([
        {
            "id": 2,
            "ts": 2000,
            "app_name": "ChatGPT Atlas",
            "window_title": "方案 A - 云文档",
            "ocr_text": "doc a part 2",
            "ax_text": "",
            "url": None,
        }
    ]))

    conn = sqlite3.connect(db_path)
    linked_timeline = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    conn.close()

    assert ok is True
    assert linked_timeline != 1
    assert timeline_count == 2


def test_process_knowledge_vectorization_passes_semantic_metadata(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    class _StubWorker:
        _model = object()

        async def handle(self, req):
            class _Result:
                vectors = [[0.1, 0.2, 0.3]]

            class _Response:
                status = "ok"
                result = _Result()
                error = None

            return _Response()

    storage = _StubVectorStorage()
    monkeypatch.setattr(processor, "_get_embed_worker", lambda: _StubWorker())

    import background_processor as bp_module
    monkeypatch.setattr(bp_module, "time", type("_T", (), {"time": staticmethod(lambda: 1.0)}))
    monkeypatch.setattr("embedding.vector_storage.get_vector_storage", lambda: storage)

    group = [{"id": 10, "app_name": "Gemini", "window_title": "Gemini"}]
    knowledge = {
        "overview": "今天问了 Gemini 发布计划",
        "details": "确认了发布时间窗口",
        "entities": "[\"Gemini\", \"发布计划\"]",
        "start_time": 1000,
        "end_time": 2000,
        "observed_at": 2000,
        "event_time_start": 1500,
        "event_time_end": 1800,
        "history_view": False,
        "content_origin": "live_interaction",
        "activity_type": "ask_ai",
        "is_self_generated": False,
        "evidence_strength": "medium",
        "frag_app_name": "Gemini",
        "frag_win_title": "Gemini",
        "category": "聊天",
    }

    ok = asyncio.run(processor._process_knowledge_vectorization(group, 77, knowledge))

    assert ok is True
    assert len(storage.calls) == 1
    metadata = storage.calls[0]["metadata"]
    assert metadata["source_type"] == "knowledge"
    assert metadata["knowledge_id"] == 77
    assert metadata["observed_at"] == 2000
    assert metadata["event_time_start"] == 1500
    assert metadata["event_time_end"] == 1800
    assert metadata["history_view"] is False
    assert metadata["content_origin"] == "live_interaction"
    assert metadata["activity_type"] == "ask_ai"
    assert metadata["evidence_strength"] == "medium"


def test_extraction_status_heartbeat_refreshes_updated_at(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    class _FakeClock:
        ticks = iter([1000.0, 1005.0])

        @staticmethod
        def time():
            return next(_FakeClock.ticks)

    monkeypatch.setattr("background_processor.time", _FakeClock)

    processor = BackgroundProcessor(db_path=db_path)
    status_file = tmp_path / ".memory-bread" / "state" / "extraction_status.json"
    initial = json.loads(status_file.read_text())

    processor._touch_status_file()
    refreshed = json.loads(status_file.read_text())

    assert initial["running"] is True
    assert initial["updated_at_ms"] == 1000000
    assert refreshed["running"] is True
    assert refreshed["updated_at_ms"] == 1005000


def test_trigger_unified_bake_pipeline_skips_when_no_new_knowledge(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    result = asyncio.run(processor._trigger_unified_bake_pipeline(0))

    assert result == {
        "triggered": False,
        "reason": "no_new_knowledge",
    }


def test_trigger_unified_bake_pipeline_posts_to_core(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    captured = {}

    class _StubResponse:
        def read(self):
            return b'{"id":"42","status":"accepted","auto_created_count":3,"candidate_count":1,"discarded_count":0}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _StubResponse()

    monkeypatch.setenv("CORE_ENGINE_URL", "http://127.0.0.1:7070")
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 2,
        "recommended_retry_after_ms": 0,
    })
    monkeypatch.setattr(processor, "_all_inference_queues_idle", lambda: True)
    monkeypatch.setattr("background_processor.urllib_request.urlopen", _fake_urlopen)

    result = asyncio.run(processor._trigger_unified_bake_pipeline(2))

    assert captured["url"] == "http://127.0.0.1:7070/api/bake/run"
    assert captured["method"] == "POST"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {
        "trigger_reason": "knowledge_background",
        "limit": 20,
        "max_concurrency": 3,
    }
    assert captured["timeout"] == 15
    assert result == {
        "triggered": True,
        "status": "accepted",
        "run_id": "42",
        "auto_created_count": 3,
        "candidate_count": 1,
        "discarded_count": 0,
        "reason": None,
        "actionable_count": 2,
    }


def test_trigger_unified_bake_pipeline_accepts_battery_limits(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    captured = {}

    class _StubResponse:
        def read(self):
            return b'{"id":"43","status":"accepted"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _StubResponse()

    monkeypatch.setattr("background_processor.urllib_request.urlopen", _fake_urlopen)
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 1,
        "recommended_retry_after_ms": 0,
    })
    monkeypatch.setattr(processor, "_all_inference_queues_idle", lambda: True)

    result = asyncio.run(
        processor._trigger_unified_bake_pipeline(
            processed_count=1,
            limit_override=1,
            max_concurrency=1,
        )
    )

    assert captured["body"] == {
        "trigger_reason": "knowledge_background",
        "limit": 1,
        "max_concurrency": 1,
    }
    assert result["triggered"] is True


def test_trigger_unified_bake_pipeline_does_not_treat_skipped_200_as_started(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    class _StubResponse:
        def read(self):
            return b'{"id":null,"status":"skipped","reason":"max 1 concurrent bake runs reached"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "background_processor.urllib_request.urlopen",
        lambda request, timeout=0: _StubResponse(),
    )
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 1,
        "recommended_retry_after_ms": 0,
    })
    monkeypatch.setattr(processor, "_all_inference_queues_idle", lambda: True)

    result = asyncio.run(
        processor._trigger_unified_bake_pipeline(
            processed_count=1,
            limit_override=1,
            max_concurrency=1,
        )
    )

    assert result["triggered"] is False
    assert result["status"] == "skipped"
    assert result["run_id"] is None


def test_trigger_unified_bake_pipeline_defers_while_inference_is_busy(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 1,
        "recommended_retry_after_ms": 0,
    })
    monkeypatch.setattr(processor, "_all_inference_queues_idle", lambda: False)

    result = asyncio.run(
        processor._trigger_unified_bake_pipeline(
            processed_count=1,
            limit_override=1,
            max_concurrency=1,
        )
    )

    assert result == {
        "triggered": False,
        "reason": "inference_busy",
    }


def test_charging_backlog_raises_timeline_batch_limit_without_dropping_items(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    profile = type(
        "_Profile",
        (),
        {"mode": "charging", "timeline_batch_size": 20},
    )()

    assert processor._timeline_batch_limit(profile, 20) == 20
    assert processor._timeline_batch_limit(profile, 63) == 63
    assert processor._timeline_batch_limit(profile, 500) == 100


def test_battery_backlog_keeps_rate_limited_batch_size(tmp_path) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    profile = type(
        "_Profile",
        (),
        {"mode": "battery", "timeline_batch_size": 4},
    )()

    assert processor._timeline_batch_limit(profile, 500) == 4


def test_periodic_bake_check_uses_core_queue_status_as_single_source(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 0,
        "dead_letter_count": 1,
    })
    assert processor._has_pending_bake_timelines() is False

    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 1,
        "dead_letter_count": 0,
    })
    assert processor._has_pending_bake_timelines() is True


def test_periodic_bake_check_runs_before_long_capture_batch(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    events = []

    profile = type(
        "_Profile",
        (),
        {
            "mode": "charging",
            "saving_enabled": True,
            "on_external_power": True,
            "battery_percent": 100.0,
            "allow_background_extraction": True,
            "timeline_interval_secs": 30,
            "timeline_batch_size": 20,
            "bake_interval_secs": 30,
            "bake_limit": 10,
            "bake_concurrency": 1,
        },
    )()

    async def _fake_periodic_bake(*, limit, max_concurrency):
        events.append(("bake", limit, max_concurrency))
        processor.running = False
        return {"triggered": True, "run_id": 44}

    async def _fake_process_batch(**kwargs):
        events.append(("capture_batch", kwargs["limit"]))
        processor.running = False
        return {"processed_count": 0, "bake_trigger": {"triggered": False}}

    async def _noop_async(*args, **kwargs):
        return {}

    monkeypatch.setattr(processor, "_enforce_runtime_guard", lambda reason: None)
    monkeypatch.setattr(processor, "_drain_vector_deletion_queue", lambda: None)
    monkeypatch.setattr(
        processor,
        "_audit_vector_consistency",
        lambda: {"available": False},
    )
    monkeypatch.setattr(processor, "backfill_bake_document_vectors", _noop_async)
    monkeypatch.setattr(processor, "_capture_and_extraction_enabled", lambda: True)
    monkeypatch.setattr(processor.energy_policy, "current_profile", lambda **kwargs: profile)
    monkeypatch.setattr(processor, "_count_unprocessed_captures", lambda: 100)
    monkeypatch.setattr(processor, "_maybe_trigger_periodic_bake", _fake_periodic_bake)
    monkeypatch.setattr(processor, "_process_batch", _fake_process_batch)
    monkeypatch.setattr(processor, "_trigger_data_extraction", _noop_async)
    monkeypatch.setattr("background_processor.asyncio.sleep", _noop_async)

    asyncio.run(processor.run())

    assert events == [("bake", 10, 1)]


def test_large_bake_backlog_runs_bounded_burst_before_capture_turn(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    profile = type(
        "_Profile",
        (),
        {
            "mode": "charging",
            "bake_interval_secs": 30,
            "bake_limit": 10,
            "bake_concurrency": 1,
        },
    )()
    results = iter([
        {"triggered": True, "actionable_count": 374},
        {"triggered": False, "reason": "run_in_progress", "actionable_count": 374},
        {"triggered": True, "actionable_count": 364},
        {"triggered": False, "reason": "run_in_progress", "actionable_count": 364},
        {"triggered": True, "actionable_count": 354},
    ])

    async def _fake_periodic_bake(*, limit, max_concurrency):
        return next(results)

    monkeypatch.setattr(processor, "_maybe_trigger_periodic_bake", _fake_periodic_bake)

    holds = []
    for now in range(5):
        _, hold = asyncio.run(
            processor._run_periodic_bake_check(profile, 0.0, now=float(now))
        )
        holds.append(hold)

    assert holds == [True, True, True, True, False]
    assert processor._consecutive_backlog_bake_runs == 3


def test_trigger_unified_bake_pipeline_skips_core_backoff_before_model_queue(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    idle_check = lambda: (_ for _ in ()).throw(AssertionError("不应检查模型队列"))
    monkeypatch.setattr(processor, "_all_inference_queues_idle", idle_check)
    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 0,
        "recommended_retry_after_ms": 180_000,
    })

    result = asyncio.run(processor._trigger_unified_bake_pipeline(10, force=True))

    assert result["triggered"] is False
    assert result["reason"] == "no_actionable_bake_candidate"
    assert result["retry_after_ms"] == 180_000


def test_trigger_unified_bake_pipeline_ignores_retry_hint_when_work_is_actionable(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    posted = {"count": 0}

    class _StubResponse:
        def read(self):
            return b'{"id":"44","status":"accepted"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        posted["count"] += 1
        return _StubResponse()

    monkeypatch.setattr(processor, "_get_bake_queue_status", lambda: {
        "capture_enabled": True,
        "actionable_count": 378,
        "recent_no_progress_count": 5,
        "recommended_retry_after_ms": 240_000,
    })
    monkeypatch.setattr(processor, "_has_active_bake_run", lambda: False)
    monkeypatch.setattr(processor, "_all_inference_queues_idle", lambda: True)
    monkeypatch.setattr("background_processor.urllib_request.urlopen", _fake_urlopen)

    result = asyncio.run(processor._trigger_unified_bake_pipeline(10, force=True))

    assert posted["count"] == 1
    assert result["triggered"] is True
    assert result["run_id"] == "44"


def test_battery_idle_check_requires_local_and_model_api_queues_idle(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    class _LocalQueue:
        def __init__(self, idle: bool) -> None:
            self.idle = idle

        def is_idle(self) -> bool:
            return self.idle

    class _StubResponse:
        def __init__(self, idle: bool) -> None:
            self.idle = idle

        def read(self):
            return json.dumps({"status": "ok", "idle": self.idle}).encode()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    local = _LocalQueue(idle=True)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: local)
    monkeypatch.setattr(
        "background_processor.urllib_request.urlopen",
        lambda request, timeout=0: _StubResponse(idle=True),
    )
    assert processor._all_inference_queues_idle() is True

    monkeypatch.setattr(
        "background_processor.urllib_request.urlopen",
        lambda request, timeout=0: _StubResponse(idle=False),
    )
    assert processor._all_inference_queues_idle() is False

    local.idle = False
    assert processor._all_inference_queues_idle() is False


def test_battery_idle_check_fails_closed_when_model_api_unavailable(
    tmp_path, monkeypatch
) -> None:
    db_path = str(tmp_path / "captures.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)

    class _LocalQueue:
        @staticmethod
        def is_idle() -> bool:
            return True

    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _LocalQueue())
    monkeypatch.setattr(
        "background_processor.urllib_request.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(OSError("offline")),
    )

    assert processor._all_inference_queues_idle() is False


def test_document_url_with_substantive_body_gets_deterministic_metadata() -> None:
    knowledge = {
        "category": "其他",
        "importance": 2,
        "activity_type": None,
        "content_origin": None,
        "evidence_strength": None,
    }
    captures = [{
        "url": "https://docs.corp.kuaishou.com/k/home/space/document-id",
        "ax_text": "文档正文" * 80,
        "ocr_text": "",
    }]

    applied = BackgroundProcessor._apply_document_metadata_defaults(knowledge, captures)

    assert applied is True
    assert knowledge == {
        "category": "文档",
        "importance": 2,
        "activity_type": "reading",
        "content_origin": "document_reference",
        "evidence_strength": "medium",
    }


def test_document_metadata_fallback_requires_substantive_body() -> None:
    knowledge = {"category": "其他", "importance": 2}

    applied = BackgroundProcessor._apply_document_metadata_defaults(
        knowledge,
        [{
            "url": "https://docs.corp.kuaishou.com/k/home/space/document-id",
            "ax_text": "仅有标题",
        }],
    )

    assert applied is False
    assert knowledge == {"category": "其他", "importance": 2}


# ─────────────────────────────────────────────────────────────────────
# 过度合并防护：时间线增长上限守卫（针对 ID 2148 类脏数据的根治）
# ─────────────────────────────────────────────────────────────────────


def test_timeline_accepts_merge_growth_guard(tmp_path) -> None:
    """直接验证增长上限守卫的三类拦截条件。"""
    db_path = str(tmp_path / "guard.db")
    _init_db(db_path)
    processor = BackgroundProcessor(db_path=db_path)
    conn = sqlite3.connect(db_path)

    def _upsert(occurrence: int, capture_ids: str, start: int, end: int) -> None:
        conn.execute("DELETE FROM timelines WHERE id = 1")
        conn.execute(
            "INSERT INTO timelines (id, capture_id, summary, occurrence_count, "
            "capture_ids, start_time, end_time) VALUES (1, 1, 't', ?, ?, ?, ?)",
            (occurrence, capture_ids, start, end),
        )
        conn.commit()

    # 正常的小时间线：允许合并
    _upsert(1, '[1]', 1000, 1100)
    assert processor._timeline_accepts_merge(conn, 1, 3) is True

    # 合并次数达到上限：拒绝
    _upsert(_TIMELINE_MAX_OCCURRENCE_COUNT, '[1]', 1000, 1100)
    assert processor._timeline_accepts_merge(conn, 1, 1) is False

    # 成员数将超上限：拒绝
    many_ids = json.dumps(list(range(1, _TIMELINE_MAX_MEMBER_COUNT + 1)))
    _upsert(2, many_ids, 1000, 1100)
    assert processor._timeline_accepts_merge(conn, 1, 1) is False

    # 时间跨度超上限：拒绝
    span_ms = int((_TIMELINE_MAX_SPAN_HOURS + 1) * 3600000)
    _upsert(2, '[1]', 1000, 1000 + span_ms)
    assert processor._timeline_accepts_merge(conn, 1, 1) is False

    conn.close()


def _seed_plain_timeline(
    conn: sqlite3.Connection, occurrence_count: int, entities_json: str = "[]"
) -> int:
    """构造一条非文档类型的时间线（无 URL），模拟宽泛主题的代码/聊天时间线。"""
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (1, 1000, 'ChatGPT', 'ChatGPT', 'seed', '', 1, NULL)"
    )
    conn.execute(
        """
        INSERT INTO timelines (
            id, capture_id, summary, overview, details, entities, category, importance,
            occurrence_count, capture_ids, start_time, end_time, time_range_start,
            time_range_end, observed_at, content_origin, activity_type,
            evidence_strength, created_at_ms, updated_at_ms
        )
        VALUES (1, 1, '设计架构', '设计 MemoryBread 架构', 'old', ?, '代码', 4,
                ?, '[1]', 1000, 1100, 1000, 1100, 1100, NULL, NULL, NULL, 1000, 1000)
        """,
        (entities_json, occurrence_count),
    )
    conn.commit()
    return 1


def _run_plain_group_merge(db_path: str, monkeypatch) -> None:
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _SimilarExtractor(1))
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    asyncio.run(processor._process_capture_group([
        {
            "id": 2,
            "ts": 2000,
            "app_name": "ChatGPT",
            "window_title": "ChatGPT",
            "ocr_text": "新的无关内容",
            "ax_text": "",
            "url": None,
        }
    ]))


def test_overgrown_timeline_forces_new_timeline(tmp_path, monkeypatch) -> None:
    """已达合并次数上限的时间线，新的相似片段应新建时间线而非继续合并。"""
    db_path = str(tmp_path / "overgrown.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    _seed_plain_timeline(conn, _TIMELINE_MAX_OCCURRENCE_COUNT)
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (2, 2000, 'ChatGPT', 'ChatGPT', '新的无关内容', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    _run_plain_group_merge(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    linked = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    ids1 = conn.execute("SELECT capture_ids, occurrence_count FROM timelines WHERE id = 1").fetchone()
    conn.close()

    # 新片段落入新时间线，原时间线不被继续吞噬
    assert linked != 1
    assert timeline_count == 2
    assert json.loads(ids1[0]) == [1]
    assert ids1[1] == _TIMELINE_MAX_OCCURRENCE_COUNT


def test_small_plain_timeline_still_merges(tmp_path, monkeypatch) -> None:
    """回归保障：未达上限的小时间线仍能正常合并，守卫不误伤。"""
    db_path = str(tmp_path / "small.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    _seed_plain_timeline(conn, 1)
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (2, 2000, 'ChatGPT', 'ChatGPT', '相关内容', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    _run_plain_group_merge(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    linked = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    ids1, occ1 = conn.execute("SELECT capture_ids, occurrence_count FROM timelines WHERE id = 1").fetchone()
    conn.close()

    assert linked == 1
    assert timeline_count == 1
    assert json.loads(ids1) == [1, 2]
    assert occ1 == 2


# ---------------------------------------------------------------------------
# data_pages 分类搭车时间线推理：_register_timeline_data_pages
# ---------------------------------------------------------------------------


class _FakeRegisteredResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _data_pages_knowledge(pages: list) -> dict:
    return {
        "data_page_contract": "timeline-data-page.v1",
        "data_pages": pages,
        "observed_at": 1700000000000,
    }


def _gpu_capture_group() -> list:
    return [
        {
            "id": 21603,
            "ts": 1700000000000,
            "app_name": "Google Chrome",
            "window_title": "电商GPU信息平台 - GPU使用情况一览",
            "url": "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info",
            "webpage_title": "电商GPU信息平台 - GPU使用情况一览",
        }
    ]


def test_register_data_pages_posts_in_group_report_url(tmp_path, monkeypatch) -> None:
    """模型输出含本组 URL 的 data_report 页面时，注册请求携带完整契约字段。"""
    import background_processor as bp

    requests_seen = []

    def _fake_urlopen(request, timeout=None):
        requests_seen.append((request, timeout))
        return _FakeRegisteredResponse({"status": "registered", "source_id": 9, "created": True})

    monkeypatch.setattr(bp.urllib_request, "urlopen", _fake_urlopen)
    processor = BackgroundProcessor(db_path=str(tmp_path / "pages.db"))
    knowledge = _data_pages_knowledge([
        {
            "url": "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info",
            "page_kind": "data_report",
            "title": "电商GPU信息平台",
        }
    ])

    processor._register_timeline_data_pages(2160, knowledge, _gpu_capture_group())

    assert len(requests_seen) == 1
    request, _ = requests_seen[0]
    assert request.full_url.endswith("/api/data/sources/discovered")
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["capture_id"] == 21603
    assert payload["timeline_id"] == 2160
    assert payload["page_kind"] == "data_report"
    assert payload["url"] == "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info"


def test_register_data_pages_rejects_hallucinated_url(tmp_path, monkeypatch) -> None:
    """URL 不在本组 capture 集合（模型幻觉）时不发起注册请求。"""
    import background_processor as bp

    def _fail_urlopen(request, timeout=None):
        raise AssertionError("幻觉 URL 不应发起注册请求")

    monkeypatch.setattr(bp.urllib_request, "urlopen", _fail_urlopen)
    processor = BackgroundProcessor(db_path=str(tmp_path / "pages.db"))
    knowledge = _data_pages_knowledge([
        {
            "url": "https://not-in-group.example.com/dashboard",
            "page_kind": "data_report",
            "title": "幻觉页面",
        }
    ])

    processor._register_timeline_data_pages(2160, knowledge, _gpu_capture_group())


def test_register_data_pages_skips_data_content_and_missing_contract(tmp_path, monkeypatch) -> None:
    """data_content 走 data_facts 通道不注册；缺契约版本时不动作。"""
    import background_processor as bp

    def _fail_urlopen(request, timeout=None):
        raise AssertionError("data_content / 无契约不应发起注册请求")

    monkeypatch.setattr(bp.urllib_request, "urlopen", _fail_urlopen)
    processor = BackgroundProcessor(db_path=str(tmp_path / "pages.db"))

    content_only = _data_pages_knowledge([
        {
            "url": "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info",
            "page_kind": "data_content",
            "title": "含数据的普通页面",
        }
    ])
    processor._register_timeline_data_pages(2160, content_only, _gpu_capture_group())

    no_contract = _data_pages_knowledge([
        {
            "url": "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info",
            "page_kind": "data_report",
            "title": "缺契约",
        }
    ])
    no_contract.pop("data_page_contract")
    processor._register_timeline_data_pages(2160, no_contract, _gpu_capture_group())


def test_register_data_pages_survives_core_engine_outage(tmp_path, monkeypatch) -> None:
    """core-engine 接口不可用时仅告警，不抛出异常打断时间线主流程。"""
    import background_processor as bp

    def _down_urlopen(request, timeout=None):
        raise ConnectionError("core engine down")

    monkeypatch.setattr(bp.urllib_request, "urlopen", _down_urlopen)
    processor = BackgroundProcessor(db_path=str(tmp_path / "pages.db"))
    knowledge = _data_pages_knowledge([
        {
            "url": "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info",
            "page_kind": "data_platform",
            "title": "电商GPU信息平台",
        }
    ])

    # 不抛异常即为主流程容错通过
    processor._register_timeline_data_pages(2160, knowledge, _gpu_capture_group())


def test_validated_data_pages_accepts_in_group_urls_only() -> None:
    """提炼层校验：只接受结构完整、http(s) 且逐字存在于本组采集的条目。"""
    from knowledge.extractor_v2 import _validated_data_pages

    allowed = {"https://gpu.example.com/info"}
    accepted = _validated_data_pages(
        [
            {"url": "https://gpu.example.com/info", "page_kind": "data_report", "title": "GPU"},
            {"url": "https://ghost.example.com/x", "page_kind": "data_report", "title": "幻觉"},
            {"url": "https://gpu.example.com/info", "page_kind": "wrong_kind", "title": "非法类型"},
            {"url": "javascript:alert(1)", "page_kind": "data_report", "title": "非http"},
            "not-a-dict",
        ],
        allowed,
    )
    assert accepted == [
        {"url": "https://gpu.example.com/info", "page_kind": "data_report", "title": "GPU"}
    ]
    assert _validated_data_pages(
        [{"url": "https://gpu.example.com/info", "page_kind": "data_report", "title": ""}],
        set(),
    ) == []


def test_merged_blocks_expose_page_url_for_classification() -> None:
    """合并块头部与单条 prompt 均注入页面 URL，模型才能逐字引用。"""
    from knowledge.extractor_v2 import (
        DATA_PAGE_PROMPT,
        MERGE_SYSTEM_PROMPT,
        _normalize_page_url,
    )

    assert "data_pages" in DATA_PAGE_PROMPT
    assert "data_report" in DATA_PAGE_PROMPT
    assert "data_platform" in DATA_PAGE_PROMPT
    assert "data_content" in DATA_PAGE_PROMPT
    assert "页面URL" in DATA_PAGE_PROMPT
    # 规范化去掉尾部斜杠，保证模型输出与 capture URL 可比对
    assert _normalize_page_url(" https://gpu.example.com/info/ ") == "https://gpu.example.com/info"
    assert MERGE_SYSTEM_PROMPT  # 主 prompt 保留不变


class _DiscardingExtractor:
    """模拟提炼器确定性丢弃（SKIP/无价值/质量不足）的场景。"""

    def extract_merged(self, captures, preempt_check=None):
        return {"_discarded": True, "discard_reason": "no_value"}


def test_discarded_captures_consumed_into_hidden_sink(tmp_path, monkeypatch) -> None:
    """确定性丢弃的 captures 必须被标记已处理，不再无限重提炼。

    旧行为：提炼返回 None，captures.timeline_id 保持 NULL，每轮调度反复拉起。
    新行为：挂到隐藏的低价值回收时间线（is_self_generated=1，UI 不展示）。
    """
    db_path = str(tmp_path / "discarded.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (5, 5000, 'Amphetamine', 'Amphetamine', '☕', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _DiscardingExtractor())
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    handled = asyncio.run(processor._process_capture_group([
        {
            "id": 5,
            "ts": 5000,
            "app_name": "Amphetamine",
            "window_title": "Amphetamine",
            "ocr_text": "☕",
            "ax_text": "",
            "url": None,
        }
    ]))

    conn = sqlite3.connect(db_path)
    linked = conn.execute("SELECT timeline_id FROM captures WHERE id = 5").fetchone()[0]
    sink = conn.execute(
        "SELECT id, is_self_generated, overview FROM timelines WHERE summary = ?",
        ("低价值采集回收",),
    ).fetchone()
    conn.close()

    assert handled is True
    assert sink is not None
    assert linked == sink[0]
    assert sink[1] == 1          # 隐藏，UI/bake 查询带 is_self_generated=0 过滤
    assert sink[2] is None       # overview 为空，不会被相似度合并命中


def test_discarded_sink_reused_not_duplicated(tmp_path, monkeypatch) -> None:
    """多次丢弃复用同一条回收时间线，不重复创建。"""
    db_path = str(tmp_path / "discarded2.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    for cid in (5, 6):
        conn.execute(
            "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
            "timeline_id, url) VALUES (?, ?, 'Amphetamine', 'Amphetamine', '☕', '', NULL, NULL)",
            (cid, cid * 1000),
        )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _DiscardingExtractor())
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    for cid in (5, 6):
        asyncio.run(processor._process_capture_group([
            {
                "id": cid,
                "ts": cid * 1000,
                "app_name": "Amphetamine",
                "window_title": "Amphetamine",
                "ocr_text": "☕",
                "ax_text": "",
                "url": None,
            }
        ]))

    conn = sqlite3.connect(db_path)
    sink_count = conn.execute(
        "SELECT COUNT(*) FROM timelines WHERE summary = ?", ("低价值采集回收",)
    ).fetchone()[0]
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT timeline_id FROM captures WHERE id IN (5, 6)"
        ).fetchall()
    ]
    conn.close()

    assert sink_count == 1
    assert ids[0] == ids[1]
    assert ids[0] is not None


class _MixedGroupExtractor(_SimilarExtractor):
    """模拟同组混合高/低价值：提炼产出里带 _discarded_capture_ids。"""

    def __init__(self) -> None:
        super().__init__(similar_id=None)

    def extract_merged(self, captures, preempt_check=None):
        knowledge = {
            "capture_ids": json.dumps([6]),
            "summary": "万擎平台稳定性设计",
            "overview": "整理万擎平台稳定性设计与调度策略",
            "details": "有效内容",
            "entities": json.dumps([]),
            "category": "其他",
            "importance": 3,
            "occurrence_count": 1,
            "start_time": captures[0]["ts"],
            "end_time": captures[-1]["ts"],
            "duration_minutes": 0,
            "time_range_start": captures[0]["ts"],
            "time_range_end": captures[-1]["ts"],
            "key_timestamps": json.dumps([]),
            "frag_app_name": captures[-1].get("app_name"),
            "frag_win_title": captures[-1].get("window_title"),
            "observed_at": captures[-1]["ts"],
            "content_origin": None,
            "activity_type": None,
            "is_self_generated": False,
            "evidence_strength": None,
            "_discarded_capture_ids": [5],
        }
        return knowledge


def test_mixed_group_discarded_captures_excluded_from_timeline(tmp_path, monkeypatch) -> None:
    """同组混入低价值 capture 时：丢弃的进回收时间线，不写进新时间线成员。

    复现 24059（Amphetamine 菜单截图）与真实工作 capture 同组提炼后
    被一起写进真实时间线成员的污染路径。
    """
    db_path = str(tmp_path / "mixed.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (5, 5000, 'Amphetamine', 'Amphetamine', '☕', '', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (6, 6000, 'Qoder', 'Quest', '真实工作内容', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(processor, "_get_knowledge_extractor", lambda: _MixedGroupExtractor())
    monkeypatch.setattr(processor, "_process_knowledge_vectorization", _skip_vectorization)
    monkeypatch.setattr("inference_queue.get_global_queue", lambda: _ImmediateQueue())

    asyncio.run(processor._process_capture_group([
        {
            "id": 5,
            "ts": 5000,
            "app_name": "Amphetamine",
            "window_title": "Amphetamine",
            "ocr_text": "☕",
            "ax_text": "",
            "url": None,
        },
        {
            "id": 6,
            "ts": 6000,
            "app_name": "Qoder",
            "window_title": "Quest",
            "ocr_text": "真实工作内容",
            "ax_text": "",
            "url": None,
        },
    ]))

    conn = sqlite3.connect(db_path)
    link5 = conn.execute("SELECT timeline_id FROM captures WHERE id = 5").fetchone()[0]
    link6 = conn.execute("SELECT timeline_id FROM captures WHERE id = 6").fetchone()[0]
    sink = conn.execute(
        "SELECT id, is_self_generated FROM timelines WHERE summary = ?",
        ("低价值采集回收",),
    ).fetchone()
    real = conn.execute(
        "SELECT id, capture_ids FROM timelines WHERE summary != ?",
        ("低价值采集回收",),
    ).fetchone()
    conn.close()

    assert sink is not None and sink[1] == 1
    assert link5 == sink[0]          # 丢弃 capture 进隐藏回收时间线
    assert real is not None
    assert link6 == real[0]          # 有效 capture 正常成线
    assert json.loads(real[1]) == [6]  # 时间线成员不含被丢弃的 capture


def test_similarity_merge_blocked_when_entities_disjoint(tmp_path, monkeypatch) -> None:
    """实体一致性守卫：新旧知识实体零交集时拒绝合并，避免"同项目不同任务"互串。

    复现 timeline 2713 脏数据场景：两个措辞相近但主题不同的排查任务
    被相似度去重误合并，导致大量无关采集记录并入同一时间线。
    """
    db_path = str(tmp_path / "disjoint.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    _seed_plain_timeline(conn, 1, entities_json='["MemoryBread", "ID1230"]')
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (2, 2000, 'ChatGPT', 'ChatGPT', '另一个任务的内容', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    _run_plain_group_merge(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    linked = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    ids1, occ1 = conn.execute(
        "SELECT capture_ids, occurrence_count FROM timelines WHERE id = 1"
    ).fetchone()
    conn.close()

    # 新知识实体 [万擎, SLO] 与已有 [MemoryBread, ID1230] 零交集 → 新建时间线
    assert linked != 1
    assert timeline_count == 2
    assert json.loads(ids1) == [1]
    assert occ1 == 1


def test_similarity_merge_allowed_when_entities_overlap(tmp_path, monkeypatch) -> None:
    """实体有交集时守卫不误伤，相似度合并正常进行。"""
    db_path = str(tmp_path / "overlap.db")
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    _seed_plain_timeline(conn, 1, entities_json='["万擎", "SLO"]')
    conn.execute(
        "INSERT INTO captures (id, ts, app_name, win_title, ocr_text, ax_text, "
        "timeline_id, url) VALUES (2, 2000, 'ChatGPT', 'ChatGPT', '相关内容', '', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    _run_plain_group_merge(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    linked = conn.execute("SELECT timeline_id FROM captures WHERE id = 2").fetchone()[0]
    timeline_count = conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0]
    ids1, occ1 = conn.execute(
        "SELECT capture_ids, occurrence_count FROM timelines WHERE id = 1"
    ).fetchone()
    conn.close()

    assert linked == 1
    assert timeline_count == 1
    assert json.loads(ids1) == [1, 2]
    assert occ1 == 2
