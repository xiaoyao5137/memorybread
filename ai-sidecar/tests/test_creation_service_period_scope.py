"""An explicitly confirmed calendar period must constrain every data path."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from creation.prompt_evidence import CreationEvidencePrompts
from creation.service import CreationOptions, CreationService


def _period(start_day: int, end_day: int) -> dict:
    tz = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 9, start_day, tzinfo=tz)
    end = datetime(2026, 9, end_day, 23, 59, 59, 999000, tzinfo=tz)
    return {
        "has_relative_time": False,
        "has_absolute_time": True,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "period_start_ms": int(start.timestamp() * 1000),
        "period_end_ms": int(end.timestamp() * 1000),
        "timezone": "Asia/Shanghai",
    }


def _snapshot(text: str, start_day: int, end_day: int) -> dict:
    period = _period(start_day, end_day)
    return {
        "content_text": text,
        "structured_data": {
            "period": {
                "start_at": period["period_start_ms"],
                "end_at": period["period_end_ms"],
            },
            "metric_rows": [{"metric": "GPU 费用", "value": text, "statement": text}],
        },
    }


def test_absolute_period_selects_target_snapshot_without_comparable_history():
    target = _period(21, 27)
    later = _snapshot("2026-09-29 其他系统执行耗时 402ms", 28, 30)
    in_period = _snapshot("2026-09-23 GPU 费用为 100 元", 21, 27)
    comparable = _snapshot("2026-09-18 GPU 费用为 200 元", 14, 20)
    source = {
        "source_id": 14840,
        "source_kind": "work_memory",
        "can_use": True,
        "content_excerpt": later["content_text"],
        "structured_data": later["structured_data"],
        "history": [later, in_period, comparable],
    }

    result = CreationService._apply_requested_period_policy([source], target)[0]
    writer_view = CreationEvidencePrompts._prompt_data_results([result])

    assert result["can_use"] is True
    assert result["period_match"] is True
    assert result["content_excerpt"] == "2026-09-23 GPU 费用为 100 元"
    assert "402ms" not in str(writer_view)
    assert "200 元" not in str(writer_view)
    assert "100 元" in str(writer_view)


def test_absolute_period_rejects_source_with_only_other_or_comparable_periods():
    later = _snapshot("2026-09-29 其他系统执行耗时 402ms", 28, 30)
    comparable = _snapshot("2026-09-18 GPU 费用为 200 元", 14, 20)
    source = {
        "source_id": 14840,
        "source_kind": "work_memory",
        "can_use": True,
        "content_excerpt": later["content_text"],
        "structured_data": later["structured_data"],
        "history": [later, comparable],
    }

    result = CreationService._apply_requested_period_policy(
        [source], _period(21, 27)
    )[0]

    assert result["can_use"] is False
    assert result["period_match"] is False
    assert result["unavailable_reason"] == "requested_period_mismatch"
    assert result["content_excerpt"] is None
    assert result["history"] == []


def test_default_today_bounds_do_not_constrain_unspecified_request():
    context = CreationService._relative_time_context(
        "写一份普通方案", now=datetime(2026, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert context["has_relative_time"] is False
    assert context["period_start_ms"] > 0
    assert CreationService._requested_period_bounds(context) is None
    assert CreationService._report_collection_period(context) == {}


def test_collection_week_does_not_verify_old_or_undated_metric():
    target = _period(21, 27)
    captured_this_week = {
        "start_at": target["period_start_ms"],
        "end_at": target["period_end_ms"],
    }
    for statement, reason in (
        ("2026-09-18 GPU 费用为 200 元", "requested_period_mismatch"),
        ("本周 GPU 费用为 200 元", "requested_period_unverified"),
        ("采集于 2026-09-23，GPU 费用为 200 元", "requested_period_unverified"),
    ):
        source = {
            "source_kind": "work_memory", "can_use": True,
            "observed_at": target["period_start_ms"] + 1000,
            "content_excerpt": statement,
            "structured_data": {
                "period": captured_this_week,
                "metric_rows": [{"metric": "GPU 费用", "value": "200 元",
                                 "statement": statement}],
            },
            "provenance": {"period": captured_this_week},
        }
        result = CreationService._apply_requested_period_policy([source], target)[0]
        assert result["can_use"] is False
        assert result["unavailable_reason"] == reason
        assert result["content_excerpt"] is None


def test_overlapping_month_total_is_not_a_weekly_fact():
    target = _period(21, 27)
    month_total = _snapshot(
        "2026-09-01 至 2026-09-30 GPU 总费用为 300 元", 21, 27
    )
    source = {
        "source_kind": "work_memory", "can_use": True,
        "content_excerpt": month_total["content_text"],
        "structured_data": month_total["structured_data"],
        "history": [],
    }
    rejected = CreationService._apply_requested_period_policy([source], target)[0]
    assert rejected["can_use"] is False
    assert rejected["unavailable_reason"] == "requested_period_mismatch"

    weekly = _snapshot("2026-09-23 GPU 单日费用为 10 元", 21, 27)
    source["history"] = [weekly]
    selected = CreationService._apply_requested_period_policy([source], target)[0]
    writer_view = str(CreationEvidencePrompts._prompt_data_results([selected]))
    assert selected["can_use"] is True
    assert "单日费用为 10 元" in writer_view
    assert "总费用为 300 元" not in writer_view


def test_gpu_source_title_cannot_admit_unrelated_execution_metric_rows():
    target = _period(21, 27)
    query = "查询 2026-09-21 至 2026-09-27 期间的 GPU 账单及用量数据"
    rows = [
        {"metric": "DAG 循环节点耗时", "value": "402ms",
         "statement": "2026-09-23 短视频工作台 DAG 循环节点耗时 402ms"},
        {"metric": "GPU 用量", "value": "100 卡时",
         "statement": "2026-09-23 GPU 用量为 100 卡时"},
    ]
    source = {
        "title": "GPU 账单与用量", "source_kind": "work_memory", "can_use": True,
        "content_excerpt": "\n".join(row["statement"] for row in rows),
        "structured_data": {"metric_rows": rows},
    }

    selected = CreationService._apply_requested_period_policy(
        [source], target, query=query
    )[0]
    writer_view = str(CreationEvidencePrompts._prompt_data_results([selected]))
    assert selected["can_use"] is True
    assert selected["topic_match"] is True
    assert selected["matched_metric_rows"] == 1
    assert "100 卡时" in writer_view
    assert "402ms" not in writer_view

    source["structured_data"] = {"metric_rows": rows[:1]}
    source["content_excerpt"] = rows[0]["statement"]
    rejected = CreationService._apply_requested_period_policy(
        [source], target, query=query
    )[0]
    assert rejected["can_use"] is False
    assert rejected["unavailable_reason"] == "requested_metric_unmatched"
    assert rejected["content_excerpt"] is None


def test_explicit_target_metric_survives_later_collection_and_unknown_rows_do_not():
    target = _period(21, 27)
    later = _period(28, 30)
    source = {
        "source_kind": "work_memory", "can_use": True,
        "observed_at": later["period_start_ms"],
        "content_excerpt": "2026-09-23 GPU 费用 100 元；本周未知指标 8 元",
        "structured_data": {
            "period": {"start_at": later["period_start_ms"],
                       "end_at": later["period_end_ms"]},
            "metric_rows": [
                {"metric": "GPU 费用", "value": "100 元",
                 "statement": "2026-09-23 GPU 费用 100 元"},
                {"metric": "未知指标", "value": "8 元",
                 "statement": "本周未知指标 8 元"},
            ],
        },
    }
    result = CreationService._apply_requested_period_policy([source], target)[0]

    assert result["can_use"] is True
    assert result["content_excerpt"] == "2026-09-23 GPU 费用 100 元"
    assert len(result["structured_data"]["metric_rows"]) == 1
    assert "8 元" not in str(CreationEvidencePrompts._prompt_data_results([result]))


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE data_sources (
            id INTEGER PRIMARY KEY, title TEXT, source_url TEXT, status TEXT,
            deleted_at INTEGER
        );
        CREATE TABLE data_snapshots (
            id INTEGER PRIMARY KEY, source_id INTEGER, collected_at INTEGER,
            observed_at INTEGER, content_text TEXT, structured_data TEXT,
            collector TEXT, status TEXT, period_start_at INTEGER,
            period_end_at INTEGER
        );
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY, ts INTEGER, webpage_title TEXT,
            win_title TEXT, ax_text TEXT, ocr_text TEXT, input_text TEXT,
            audio_text TEXT, url TEXT, is_sensitive INTEGER
        );
        """
    )
    return conn


def test_absolute_period_memory_query_selects_matching_historical_snapshot():
    target = _period(21, 27)
    later = _period(28, 30)
    with _memory_db() as conn:
        conn.execute("INSERT INTO data_sources VALUES (1, 'GPU 费用', NULL, 'active', NULL)")
        conn.execute("INSERT INTO data_sources VALUES (2, 'GPU 费用', NULL, 'active', NULL)")
        conn.execute("INSERT INTO data_sources VALUES (3, 'GPU 费用', NULL, 'active', NULL)")
        conn.execute("INSERT INTO data_sources VALUES (4, 'GPU 费用', NULL, 'active', NULL)")
        conn.execute(
            "INSERT INTO data_snapshots VALUES (1, 1, 10, 10, '2026-09-23 目标期 GPU 费用 100 元', ?,"
            " 'memory_extract', 'success', ?, ?)",
            (json.dumps({"metric_rows": [{"metric": "GPU 费用", "value": "100 元",
                                        "statement": "2026-09-23 目标期 GPU 费用 100 元"}]}),
             target["period_start_ms"], target["period_end_ms"]),
        )
        conn.execute(
            "INSERT INTO data_snapshots VALUES (2, 1, 20, 20, '2026-09-29 下一期执行耗时 402ms', ?,"
            " 'memory_extract', 'success', ?, ?)",
            (json.dumps({"metric_rows": [{"metric": "耗时", "value": "402ms",
                                        "statement": "2026-09-29 下一期执行耗时 402ms"}]}),
             later["period_start_ms"], later["period_end_ms"]),
        )
        conn.execute(
            "INSERT INTO data_snapshots VALUES (3, 2, 20, 20, '另一来源的错期数据', '{}',"
            " 'memory_extract', 'success', ?, ?)",
            (later["period_start_ms"], later["period_end_ms"]),
        )
        for source_id, statement in (
            (3, "2026-09-18 GPU 费用为 200 元"),
            (4, "本周 GPU 费用为 200 元"),
        ):
            conn.execute(
                "INSERT INTO data_snapshots VALUES (?, ?, 30, 30, ?, ?,"
                " 'memory_extract', 'success', ?, ?)",
                (source_id + 1, source_id, statement,
                 json.dumps({"metric_rows": [{"metric": "GPU 费用", "value": "200 元",
                                             "statement": statement}]}),
                 target["period_start_ms"], target["period_end_ms"]),
            )
        service = CreationService.__new__(CreationService)
        rows = service._query_data_memory_rows(
            conn, "", {"time_context": target}, CreationOptions()
        )

    assert [row["source_id"] for row in rows] == [1]
    assert "2026-09-23 目标期 GPU 费用" in rows[0]["full_content"]
    assert "402ms" not in rows[0]["full_content"]


def test_absolute_period_pending_captures_remain_source_leads():
    target = _period(21, 27)
    later = _period(28, 30)
    with _memory_db() as conn:
        body = "GPU 费用来源记录。" * 30
        for capture_id, timestamp in (
            (1, target["period_start_ms"] + 3_600_000),
            (2, later["period_start_ms"] + 3_600_000),
        ):
            conn.execute(
                "INSERT INTO captures VALUES (?, ?, 'GPU 费用', '', ?, '', '', '', ?, 0)",
                (capture_id, timestamp, body, "https://example.invalid/" + str(capture_id)),
            )
        service = CreationService.__new__(CreationService)
        rows = service._query_pending_capture_rows(
            conn, "", {"time_context": target}, CreationOptions()
        )

    assert {row["source_id"] for row in rows} == {1, 2}
    assert all(row["review_status"] == "period_unverified" for row in rows)
    assert all("GPU 费用来源记录" not in row["full_content"] for row in rows)


def test_absolute_period_is_used_for_report_collection():
    target = _period(21, 27)
    target["current_date"] = "2026-09-30"

    assert CreationService._report_collection_period(target) == {
        "start": "2026-09-21",
        "end": "2026-09-27",
        "display": "",
    }


@pytest.mark.parametrize("absolute", [False, True])
def test_refresh_fallback_rejects_collection_week_as_statistical_period(
    tmp_path, absolute,
):
    target = _period(21, 27)
    target["has_absolute_time"] = absolute
    target["has_relative_time"] = not absolute
    db_path = tmp_path / "fallback.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE data_snapshots (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " collected_at INTEGER, observed_at INTEGER, period_start_at INTEGER,"
        " period_end_at INTEGER, content_text TEXT, structured_data TEXT, collector TEXT)"
    )
    for snapshot_id, statement in (
        (1, "2026-09-18 GPU 费用为 200 元"),
        (2, "本周 GPU 费用为 200 元"),
    ):
        conn.execute(
            "INSERT INTO data_snapshots VALUES (?, 7, ?, ?, ?, ?, ?, ?, 'browser_attach')",
            (snapshot_id, target["period_start_ms"] + snapshot_id,
             target["period_start_ms"] + snapshot_id,
             target["period_start_ms"], target["period_end_ms"], statement,
             json.dumps({"metric_rows": [{"metric": "GPU 费用", "value": "200 元",
                                         "statement": statement}]})),
        )
    conn.commit()
    assert CreationService._stale_snapshot_fallback_info(str(db_path), 7, target) is None

    statement = "2026-09-23 GPU 费用为 100 元"
    conn.execute(
        "INSERT INTO data_snapshots VALUES (3, 7, ?, ?, ?, ?, ?, ?, 'browser_attach')",
        (target["period_end_ms"] + 86_400_000,
         target["period_end_ms"] + 86_400_000,
         target["period_start_ms"], target["period_end_ms"],
         "旧数值 200 元；" + statement,
         json.dumps({"metric_rows": [{"metric": "GPU 费用", "value": "100 元",
                                     "statement": statement}],
                     "summary": "旧数值 200 元"})),
    )
    conn.commit()
    conn.close()
    fallback = CreationService._stale_snapshot_fallback_info(str(db_path), 7, target)
    assert fallback is not None
    assert fallback["content_excerpt"] == statement
    assert fallback["structured_data"]["metric_rows"][0]["value"] == "100 元"
    assert "200 元" not in str(fallback)
