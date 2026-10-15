import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from creation.brainstorm_fuzzy_documents import (
    fuzzy_document_candidates, fuzzy_title_score,
)
from creation.brainstorm_input_preflight import _local_timezone_label
from creation.brainstorm_stages import BrainstormStageCoordinator


def _document_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE bake_documents (id INTEGER PRIMARY KEY,title TEXT,status TEXT,"
            "deleted_at INTEGER,full_content TEXT,summary TEXT,review_status TEXT,"
            "source_url TEXT,updated_at INTEGER,last_refresh_completeness TEXT,"
            "last_refresh_truncated INTEGER,last_refresh_success_at_ms INTEGER)"
        )
        conn.executemany(
            "INSERT INTO bake_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (783, "大模型成本性能优化周会 周报 - 云文档", "enabled", None,
                 "会议日期：2026-09-22\n正文", "摘要", "enabled",
                 "https://example.test/document/783", 1000, "partial", 0, 1000),
                (78, "模型数据 - 周会会议纪要 -6月", "enabled", None,
                 "旧正文", "旧摘要", "enabled", None, 100, "complete", 0, 100),
                (14, "大模型成本性能优化周会 周报 - 云文档", "disabled", None,
                 "隐藏正文", "", "enabled", None, 2000, "complete", 0, 2000),
            ],
        )


def test_fuzzy_title_recall_handles_swapped_terms_without_case_aliases(tmp_path):
    path = tmp_path / "memory.db"
    _document_db(path)
    query = "大模型性能成本优化周会会议纪要"
    assert fuzzy_title_score(query, "大模型成本性能优化周会 周报") > 0.60
    result = fuzzy_document_candidates(str(path), query, 2)
    assert [item["source_id"] for item in result] == [783, 78]
    assert all(item["source_type"] == "document" for item in result)
    assert all(len(item["full_content"]) <= 12000 for item in result)
    assert fuzzy_document_candidates(str(path), "周报", 2) == []
    assert fuzzy_document_candidates(str(path), "https://example.test/783", 2) == []


@pytest.mark.asyncio
async def test_existing_recall_then_fuzzy_receipt_keeps_swapped_title_in_top_four(tmp_path):
    path = tmp_path / "memory.db"
    _document_db(path)
    calls = []

    class Service:
        db_path = str(path)

        def retrieve_references(self, query, _requirement, options):
            calls.append(("ranked", query, options.max_references))
            return [SimpleNamespace(id=index, source_id=index, source_type="knowledge",
                                    title="普通记录{}".format(index), full_content="说明",
                                    review_status="complete") for index in range(1, 13)]

    skill = {"id": "weekly", "title": "大模型优化周报", "executionSteps": [
        {"id": "meeting", "title": "本周大模型性能成本优化周会会议纪要",
         "objective": "获取本周大模型性能成本优化周会会议纪要",
         "tools": ["memory_search"]}]}
    coordinator = BrainstormStageCoordinator(Service())
    result = await coordinator._input_preflight_batch(
        root_request="请生成大模型优化周报", selected_skills=[skill], decisions=[],
        brief_edits={}, user_input_revisions={}, confirmed_period=None,
        input_decisions=[], as_of_local_date="2026-09-28",
        timezone="Asia/Shanghai", attempt=1,
    )
    receipts = result["input_preflight"]["needs"][0]["receipts"]
    assert calls and calls[0][0] == "ranked"
    assert 783 in [item["source_id"] for item in receipts]
    assert len(receipts) <= 4
    assert result["input_preflight"]["period"]["status"] == "unconfirmed"


def test_timezone_label_never_returns_ambiguous_abbreviation():
    local = datetime(2026, 9, 28, 12, tzinfo=timezone(timedelta(hours=5, minutes=45), "NPT"))
    assert _local_timezone_label(local, "NPT") == "+05:45"
    assert _local_timezone_label(local, "+05:45") == "+05:45"
    shanghai = datetime(2026, 9, 28, 12, tzinfo=timezone(timedelta(hours=8), "CST"))
    assert _local_timezone_label(shanghai, "Asia/Shanghai") == "Asia/Shanghai"
    assert _local_timezone_label(shanghai, "+08:00") == "+08:00"
    assert _local_timezone_label(shanghai, "CST") != "CST"
