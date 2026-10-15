"""Source-first detail checks keep period and retrieved provenance separate."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from creation.brainstorm_input_preflight import (
    build_input_needs, reclassify_input_preflight, run_input_preflight,
)
from creation.brainstorm_input_questions import plan_input_clarification_questions


SKILL = {
    "id": "weekly-report",
    "title": "GPU成本优化周报模板",
    "executionSteps": [
        {"id": "meeting", "title": "本周性能成本优化周会会议纪要",
         "objective": "用@记忆搜索 Tool 获取本周性能成本优化周会会议纪要，并总结为列表展示",
         "tools": ["memory_search"]},
        {"id": "progress", "title": "AIGC进度总结",
         "objective": "用@记忆搜索 Tool 获取本周AIGC共建项目进展和AIGC共建项目周报里推理性能优化进展",
         "tools": ["memory_search"]},
        {"id": "metrics", "title": "GPU算力数据",
         "objective": "用@数据检索 Tool 获取平台数据：① GPU使用报表切换到周期内最后一个可用统计日；② 项目用量报表按成本排序。",
         "tools": ["data_search"]},
        {"id": "tokens", "title": "Token数据",
         "objective": "用@数据检索 Tool 获取模型中心运营看板第二个Tab下筛选本周的六项Token数据",
         "tools": ["data_search"]},
    ],
}


@pytest.mark.asyncio
async def test_v17_compute_cost_report_without_skill_requires_source_or_explicit_gap():
    root = "请生成GPU成本优化的周报"
    calls = []

    async def memory_lookup(query, _limit):
        calls.append(("memory", query))
        return []

    async def data_lookup(query, _period, _limit):
        calls.append(("data", query))
        return []

    kwargs = {"memory_allowed": True, "memory_lookup": memory_lookup,
              "data_lookup": data_lookup, "today": date(2026, 9, 30),
              "confirmed_period": {"start": "2026-09-21", "end": "2026-09-27",
                                   "source": "user"}}
    preflight = await run_input_preflight(root, [], content_mode=True,
        brief_edits={}, **kwargs)
    assert len(preflight["needs"]) == 3
    assert len({need["id"] for need in preflight["needs"]}) == 3
    assert all(need["status"] == "missing" and need["skill_id"] == ""
               for need in preflight["needs"])
    assert [item[0] for item in calls] == ["memory", "data"] * 3
    plan = plan_input_clarification_questions(root_request=root, selected_skills=[],
        confirmed_decisions=[], preflight=preflight,
        as_of_date=preflight["as_of_local_date"], timezone=preflight["timezone"])
    assert plan["status"] == "question"
    assert len(plan["questions"]) == 3
    assert [question["input_clarification"] for question in plan["questions"]] == [
        {"kind": "source_confirmation", "need_ids": [need["id"]]}
        for need in preflight["needs"]]
    assert all("文档名称、原链接、准确定位" in question["answer_template"]
               for question in plan["questions"])
    assert all({option["input_value"]["action"] for option in question["options"]} == {
        "retry_local_recall", "draft_with_gap"} for question in plan["questions"])

    v16 = await run_input_preflight(root, [], **kwargs)
    assert v16["needs"] == []
    unrelated = await run_input_preflight("写社区读书角活动介绍", [],
        content_mode=True, brief_edits={}, **kwargs)
    assert unrelated["needs"] == []
    edited_away = await run_input_preflight(root, [], content_mode=True,
        brief_edits={"root_request": "写社区读书角活动介绍"}, **kwargs)
    assert edited_away["needs"] == []


@pytest.mark.asyncio
async def test_bill_receipt_cannot_complete_usage_or_utilization_needs():
    root = "请生成GPU成本优化的周报"
    bill = {"source_kind": "report_url", "source_id": 901,
            "title": "GPU账单费用报表", "source_completeness": "complete",
            "can_use": True,
            "structured_data": {"period": {"start": "2026-09-21", "end": "2026-09-27",
                                           "origin": "source_page"}}}

    async def memory_lookup(_query, _limit):
        return []

    async def data_lookup(_query, _period, _limit):
        # Stress the source gate: the same bill is returned for all searches.
        return [bill]

    result = await run_input_preflight(root, [], content_mode=True,
        brief_edits={}, memory_allowed=True, memory_lookup=memory_lookup,
        data_lookup=data_lookup,
        confirmed_period={"start": "2026-09-21", "end": "2026-09-27",
                          "source": "user"}, today=date(2026, 9, 30))
    assert [need["status"] for need in result["needs"]] == [
        "matched", "candidate", "candidate"]
    plan = plan_input_clarification_questions(root_request=root, selected_skills=[],
        confirmed_decisions=[], preflight=result,
        as_of_date=result["as_of_local_date"], timezone=result["timezone"])
    assert plan["status"] == "question"
    assert [question["input_clarification"]["need_ids"] for question in plan["questions"]] == [
        [result["needs"][1]["id"]], [result["needs"][2]["id"]]]


def test_skill_steps_become_bounded_source_targets_without_period_assumptions():
    needs = build_input_needs([SKILL])
    assert [(item["skill_step_id"], item["label"]) for item in needs] == [
        ("meeting", "本周性能成本优化周会会议纪要"),
        ("progress", "AIGC进度总结"),
        ("metrics", "GPU使用报表"),
        ("metrics", "项目用量报表"),
        ("tokens", "Token数据"),
    ]
    assert all("本周" not in item["query"] for item in needs)
    assert all(len(item["query"]) <= 180 for item in needs)
    assert needs == build_input_needs([SKILL])


def test_independent_memory_clauses_split_into_separate_source_needs():
    skill = json.loads(json.dumps(SKILL))
    skill["executionSteps"][1]["objective"] = (
        "用@记忆搜索 Tool 获取本周AIGC共建项目的进展，以及AIGC共建项目周报里"
        "关于推理性能优化相关的进展，并以列表形式展示")
    needs = build_input_needs([skill])
    assert len(needs) == 6
    assert [need["skill_step_id"] for need in needs].count("progress") == 2
    assert any("推理性能优化" in need["query"] for need in needs)
    assert all(need["source_form"] == "document" for need in needs
               if need["skill_step_id"] == "progress")


@pytest.mark.asyncio
async def test_weekly_progress_fallback_finds_locally_named_minutes_and_progress_doc():
    skill = json.loads(json.dumps(SKILL))
    skill["executionSteps"] = [skill["executionSteps"][1]]
    skill["executionSteps"][0]["objective"] = (
        "获取本周AIGC共建项目的进展，以及AIGC共建项目周报里关于推理性能优化相关的进展")
    queries = []

    async def lookup(query, _limit):
        queries.append(query)
        if query == "AIGC共建项目周会":
            return [{"source_type": "document", "source_id": 925,
                     "title": "AIGC共建项目周会", "review_status": "partial"}]
        if query == "推理性能优化周进度总结":
            return [{"source_type": "document", "source_id": 1043,
                     "title": "推理加速小组周进度总结", "review_status": "partial"}]
        return []

    result = await run_input_preflight(
        "写AIGC项目周报", [skill], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28))
    assert [item["source_id"] for need in result["needs"] for item in need["receipts"]] == [925, 1043]
    assert queries[1] == "AIGC共建项目周会"
    assert queries[3] == "推理性能优化周进度总结"
    assert all(need["status"] == "partial" for need in result["needs"])


@pytest.mark.asyncio
async def test_memory_is_checked_before_data_and_unconfirmed_period_stays_candidate():
    calls = []

    async def memory_lookup(query, limit):
        calls.append(("memory", query, limit))
        return [{"source_type": "document", "source_id": 7, "title": "周会会议纪要",
                 "full_content": "会议日期：2026-09-22\n内容完整", "updated_at": 1790000000000,
                 "review_status": "complete"}]

    async def data_lookup(query, period, limit):
        calls.append(("data", query, limit))
        assert period["status"] == "unconfirmed"
        return [{"source_kind": "report_url", "source_id": 9, "title": "来源看板",
                 "source_url": "https://example.test/report?id=9", "collected_at": 1790600000000,
                 "structured_data": {"period": {"start": "2026-09-27", "end": "2026-09-27"}},
                 "source_completeness": "complete", "can_use": True}]

    result = await run_input_preflight(
        "请生成GPU成本优化周报", [SKILL], memory_allowed=True,
        memory_lookup=memory_lookup, data_lookup=data_lookup,
        today=date(2026, 9, 28),
    )
    assert result["period"] == {
        "status": "unconfirmed", "cadence": "weekly", "candidate_ranges": [
            {"start": "2026-09-21", "end": "2026-09-27", "kind": "latest_completed_week"},
            {"start": "2026-09-28", "end": "2026-10-04", "kind": "current_week"},
        ]}
    assert [item["status"] for item in result["needs"]] == ["candidate"] * 5
    assert [item[0] for item in calls] == [
        "memory", "memory", "memory", "data", "memory", "data", "memory", "data"]
    assert all(item["period_status"] == "unknown"
               for need in result["needs"] for item in need["receipts"])
    payload = json.dumps(result, ensure_ascii=False)
    assert "内容完整" not in payload
    assert "full_content" not in payload


@pytest.mark.asyncio
async def test_confirmed_period_requires_explicit_subject_period_and_complete_source():
    async def memory_lookup(query, _limit):
        if "会议纪要" in query:
            return [{"source_type": "document", "source_id": 7, "title": "周会会议纪要",
                     "full_content": "会议日期：2026-09-22\n纪要", "review_status": "complete"}]
        if "AIGC" in query:
            return [{"source_type": "document", "source_id": 8, "title": "AIGC 项目周报",
                     "full_content": "统计周期：2026-09-14 至 2026-09-18\n旧进展",
                     "review_status": "complete"}]
        return []

    async def data_lookup(query, _period, _limit):
        if "GPU使用" in query:
            return [{"source_kind": "report_url", "source_id": 9, "title": "GPU使用报表",
                     "collected_at": 1790600000000, "source_completeness": "complete",
                     "structured_data": {"period": {"start": "2026-09-26", "end": "2026-09-26",
                                                    "origin": "source_page"}}}]
        if "项目用量" in query:
            return [{"source_kind": "report_url", "source_id": 10, "title": "项目用量报表",
                     "collected_at": 1790600000000, "source_completeness": "partial",
                     "structured_data": {"period": {"start": "2026-09-21", "end": "2026-09-27",
                                                    "origin": "source_page"}}}]
        return [{"source_kind": "report_url", "source_id": 11, "title": "Token 看板",
                 "collected_at": 1790600000000, "source_completeness": "complete"}]

    result = await run_input_preflight(
        "请生成GPU成本优化周报", [SKILL], memory_allowed=True,
        memory_lookup=memory_lookup, data_lookup=data_lookup,
        confirmed_period={"start": "2026-09-21", "end": "2026-09-27", "source": "user"},
        today=date(2026, 9, 28),
    )
    assert result["period"]["status"] == "confirmed"
    statuses = {need["skill_step_id"] + ":" + need["label"]: need["status"] for need in result["needs"]}
    assert statuses["meeting:本周性能成本优化周会会议纪要"] == "matched"
    assert statuses["progress:AIGC进度总结"] == "candidate"
    assert statuses["metrics:GPU使用报表"] == "matched"
    assert statuses["metrics:项目用量报表"] == "partial"
    assert statuses["tokens:Token数据"] == "candidate"
    token_receipt = result["needs"][-1]["receipts"][-1]
    assert token_receipt["period_status"] == "unknown"
    assert token_receipt["observed_at"] == 1790600000000
    assert token_receipt["source_period"] is None


@pytest.mark.asyncio
async def test_explicit_user_range_and_relative_request_confirm_without_skill_guess():
    async def lookup(_query, _limit):
        return []

    result = await run_input_preflight(
        "请写 2026-09-21 至 2026-09-27 周报", [SKILL],
        memory_allowed=True, memory_lookup=lookup, today=date(2026, 9, 28),
    )
    assert result["period"]["start"] == "2026-09-21"
    assert result["period"]["end"] == "2026-09-27"
    assert result["period"]["status"] == "confirmed"
    relative = await run_input_preflight(
        "请写上周周报", [SKILL], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28),
    )
    assert relative["period"]["start"] == "2026-09-21"
    assert relative["period"]["status"] == "confirmed"
    chinese_range = await run_input_preflight(
        "写 2026年9月21日至9月27日 周报", [SKILL], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28),
    )
    assert chinese_range["period"]["end"] == "2026-09-27"
    iso_week = await run_input_preflight(
        "写 2026年第39周 周报", [SKILL], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28),
    )
    assert iso_week["period"]["start"] == "2026-09-21"


@pytest.mark.asyncio
async def test_lookup_failure_is_not_reported_as_missing_and_revoked_permission_skips_all():
    calls = []

    async def failing(_query, _limit):
        calls.append("memory")
        raise RuntimeError("private provider diagnostic must not leak")

    failed = await run_input_preflight(
        "写周报", [SKILL], memory_allowed=True,
        memory_lookup=failing, today=date(2026, 9, 28),
    )
    assert failed["status"] == "failed"
    assert all(item["status"] == "failed" for item in failed["needs"])
    assert "private provider diagnostic" not in json.dumps(failed)
    skipped = await run_input_preflight(
        "只用当前输入写周报，不要检索个人记忆", [SKILL], memory_allowed=False,
        memory_lookup=failing, today=date(2026, 9, 28),
    )
    assert skipped["status"] == "skipped"
    assert all(item["status"] == "skipped" for item in skipped["needs"])
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_sensitive_url_is_redacted_and_source_body_is_never_returned():
    skill = {"id": "once", "executionSteps": [{"id": "step", "title": "客户数据",
             "objective": "查找客户数据", "tools": ["memory_search"]}]}

    async def lookup(_query, _limit):
        return [{"source_type": "document", "source_id": "private-1", "title": "内部来源",
                 "source_url": "https://example.test/doc?id=1&authToken=private",
                 "full_content": "统计日期：2026-09-28\nprivate document body",
                 "review_status": "complete"}]

    result = await run_input_preflight(
        "写客户分析", [skill], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28),
    )
    receipt = result["needs"][0]["receipts"][0]
    assert receipt["source_url"] is None
    assert receipt["url_redacted"] is True
    assert receipt["source_period"] == {"start": "2026-09-28", "end": "2026-09-28"}
    assert receipt["period_status"] == "unknown"
    assert "authToken" not in json.dumps(result)
    assert "private document body" not in json.dumps(result)
    assert result["needs"][0]["status"] == "matched"


@pytest.mark.asyncio
async def test_saved_receipts_are_reclassified_after_user_chooses_period_without_new_lookup():
    calls = 0
    skill = {"id": "weekly", "title": "项目周报", "executionSteps": [
        {"id": "metric", "title": "项目周指标", "objective": "获取项目周指标", "tools": ["memory_search"]}]}

    async def lookup(_query, _limit):
        nonlocal calls
        calls += 1
        return [{"source_type": "document", "source_id": 1, "title": "项目周指标",
                 "full_content": "统计周期：2026-09-21 至 2026-09-27\n数值",
                 "review_status": "complete"}]

    pending = await run_input_preflight("写项目周报", [skill], memory_allowed=True,
                                        memory_lookup=lookup, today=date(2026, 9, 28))
    assert pending["needs"][0]["status"] == "candidate"
    confirmed = reclassify_input_preflight(
        pending, {"start": "2026-09-21", "end": "2026-09-27", "source": "user"})
    assert confirmed["status"] == "matched"
    assert confirmed["needs"][0]["receipts"][0]["period_status"] == "matched"
    assert pending["period"]["status"] == "unconfirmed"
    assert calls == 1


@pytest.mark.asyncio
async def test_reference_id_fallback_and_report_receipt_survive_bounded_ranking():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "report", "title": "GPU使用报表", "objective": "获取GPU使用报表",
         "tools": ["data_search"]}]}

    async def memory_lookup(_query, limit):
        assert limit == 12
        return [SimpleNamespace(id=40, source_id=None, source_type="document",
                                title="GPU使用报表来源", full_content="正文", review_status="approved")]

    async def data_lookup(_query, _period, _limit):
        unrelated = [{"source_kind": "work_memory", "source_id": index,
                      "title": "GPU相关操作记忆"} for index in range(10)]
        return unrelated + [{"source_kind": "report_url", "source_id": 1582,
                             "title": "GPU使用情况一览", "can_use": False}]

    result = await run_input_preflight("写周报", [skill], memory_allowed=True,
                                       memory_lookup=memory_lookup, data_lookup=data_lookup,
                                       today=date(2026, 9, 28))
    receipt_ids = [item["source_id"] for item in result["needs"][0]["receipts"]]
    assert 40 in receipt_ids
    assert 1582 in receipt_ids
    assert result["needs"][0]["status"] == "candidate"


@pytest.mark.asyncio
async def test_user_locator_adds_one_bounded_local_lookup_for_exact_need_only():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "meeting", "title": "项目周会纪要", "objective": "获取项目周会纪要",
         "tools": ["memory_search"]}]}
    need_id = build_input_needs([skill])[0]["id"]
    queries = []

    async def lookup(query, _limit):
        queries.append(query)
        if "用户提供定位" in query:
            return [{"source_type": "document", "source_id": 783, "title": "项目周会纪要",
                     "full_content": "会议日期：2026-09-22\n完整纪要",
                     "review_status": "complete"}]
        return []

    result = await run_input_preflight(
        "写周报", [skill], memory_allowed=True, memory_lookup=lookup,
        confirmed_period={"start": "2026-09-21", "end": "2026-09-27", "source": "user"},
        supplemental_queries={need_id: "用户提供定位" + "长" * 300,
                              "unknown_need": "不得执行的查询"},
        today=date(2026, 9, 28),
    )
    assert len(queries) == 2
    assert len(queries[1]) == 180
    assert "不得执行的查询" not in queries
    assert result["needs"][0]["status"] == "matched"
    assert result["needs"][0]["receipts"][0]["source_id"] == 783
    assert "用户提供定位" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_unavailable_data_lookup_is_not_called_missing():
    skill = {"id": "s", "executionSteps": [{"id": "metrics", "title": "运营报表",
             "objective": "获取运营报表", "tools": ["data_search"]}]}

    async def empty_memory(_query, _limit):
        return []

    result = await run_input_preflight("写周报", [skill], memory_allowed=True,
                                       memory_lookup=empty_memory, data_lookup=None,
                                       today=date(2026, 9, 28))
    assert result["needs"][0]["status"] == "failed"
    assert result["needs"][0]["lookup_status"] == "failed"


@pytest.mark.asyncio
async def test_structured_partial_overrides_successful_capture_status():
    skill = {"id": "s", "executionSteps": [{"id": "metrics", "title": "运营报表",
             "objective": "获取运营报表", "tools": ["data_search"]}]}

    async def empty_memory(_query, _limit):
        return []

    async def partial_report(_query, _period, _limit):
        return [{"source_kind": "report_url", "source_id": 7, "title": "运营报表",
                 "status": "success", "can_use": True,
                 "structured_data": {"period": {"start": "2026-09-21", "end": "2026-09-27"},
                                     "completeness": {"status": "partial", "truncated": True}}}]

    result = await run_input_preflight(
        "写2026-09-21至2026-09-27周报", [skill], memory_allowed=True,
        memory_lookup=empty_memory, data_lookup=partial_report,
        today=date(2026, 9, 28),
    )
    assert result["needs"][0]["status"] == "partial"
    assert result["needs"][0]["receipts"][0]["completeness"] == "partial"


@pytest.mark.asyncio
async def test_knowledge_summary_does_not_satisfy_required_full_minutes():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "meeting", "title": "本周会议纪要", "objective": "获取本周会议纪要",
         "tools": ["memory_search"]}]}

    async def lookup(_query, _limit):
        return [{"source_type": "knowledge", "source_id": 5,
                 "title": "会议摘要", "full_content": "会议日期：2026-09-22\n摘要",
                 "review_status": "complete"}]

    result = await run_input_preflight(
        "写2026-09-21至2026-09-27周报", [skill], memory_allowed=True,
        memory_lookup=lookup, today=date(2026, 9, 28))
    assert result["needs"][0]["source_form"] == "document"
    assert result["needs"][0]["status"] == "candidate"


@pytest.mark.asyncio
async def test_requested_snapshot_window_cannot_override_page_statistic_date():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "report", "title": "使用报表",
         "objective": "获取使用报表并选择周期内最后一个可用统计日", "tools": ["data_search"]}]}

    async def empty_memory(_query, _limit):
        return []

    async def report(_query, _period, _limit):
        return [{"source_kind": "report_url", "source_id": 7, "title": "使用报表",
                 "can_use": True, "content_text": "统计日期：2026-09-26\n指标列表",
                 "structured_data": {"period": {"start": "2026-09-28", "end": "2026-10-04"},
                                     "completeness": {"status": "complete"}}}]

    result = await run_input_preflight(
        "写2026-09-28至2026-10-04周报", [skill], memory_allowed=True,
        memory_lookup=empty_memory, data_lookup=report, today=date(2026, 9, 28))
    receipt = result["needs"][0]["receipts"][0]
    assert receipt["source_period"] == {"start": "2026-09-26", "end": "2026-09-26"}
    assert receipt["period_status"] == "mismatch"
    assert result["needs"][0]["status"] == "candidate"


@pytest.mark.asyncio
async def test_point_period_must_lie_entirely_inside_confirmed_range():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "report", "title": "使用报表",
         "objective": "获取使用报表并选择周期内最后一个可用统计日", "tools": ["data_search"]}]}

    async def empty_memory(_query, _limit):
        return []

    async def broad_report(_query, _period, _limit):
        return [{"source_kind": "report_url", "source_id": 7, "title": "使用报表",
                 "can_use": True, "structured_data": {
                     "period": {"start": "2026-09-26", "end": "2026-09-29", "origin": "source_page"},
                     "completeness": {"status": "complete"}}}]

    result = await run_input_preflight(
        "写2026-09-28至2026-10-04周报", [skill], memory_allowed=True,
        memory_lookup=empty_memory, data_lookup=broad_report, today=date(2026, 9, 28))
    assert result["needs"][0]["receipts"][0]["period_status"] == "mismatch"


@pytest.mark.asyncio
async def test_user_report_locator_runs_bounded_memory_then_data_queries():
    skill = {"id": "s", "title": "周报", "executionSteps": [
        {"id": "metrics", "title": "成本报表", "objective": "获取成本报表",
         "tools": ["data_search"]}]}
    need_id = build_input_needs([skill])[0]["id"]
    calls = []

    async def memory(query, limit):
        calls.append(("memory", query, limit))
        return []

    async def data(query, _period, limit):
        calls.append(("data", query, limit))
        if query == "用户给出的源报表标题":
            return [{"source_kind": "report_url", "source_id": 9, "title": "源报表",
                     "can_use": True, "content_text": "统计周期：2026-09-21 至 2026-09-27",
                     "structured_data": {"completeness": {"status": "complete"}}}]
        return []

    result = await run_input_preflight(
        "写2026-09-21至2026-09-27周报", [skill], memory_allowed=True,
        memory_lookup=memory, data_lookup=data,
        supplemental_queries={need_id: "用户给出的源报表标题", "unknown": "ignore"},
        today=date(2026, 9, 28))
    primary = build_input_needs([skill])[0]["query"]
    assert [(kind, query) for kind, query, _ in calls] == [
        ("memory", primary),
        ("memory", "用户给出的源报表标题"),
        ("data", primary),
        ("data", "用户给出的源报表标题"),
    ]
    assert all(limit == 12 for _, _, limit in calls)
    assert result["needs"][0]["status"] == "matched"
    assert result["needs"][0]["receipts"][0]["source_id"] == 9
    assert "用户给出的源报表标题" not in json.dumps(result, ensure_ascii=False)
