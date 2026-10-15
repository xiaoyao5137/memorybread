import copy
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from creation import skill_governance
from creation.agent_loop import CreationAgentLoop
from creation.brainstorm_gap_delivery import authorized_gap_steps
from creation.brainstorm_gap_delivery import revise_acknowledged_gap_preview
from creation.brainstorm_input_preflight import build_input_needs
from creation.operations import OperationError
from creation.service import CreationOptions
from tests.test_creation_delivery_contract import DeliveryService


ROOT = "请生成GPU成本优化的周报"
CHAPTER = "撰写第 1 章《创作正文》，按已确认的简报创作完整正文。"
PRIMARY = {"id": "weekly", "title": "GPU成本优化周报模板", "strictStructure": True,
           "executionSteps": [
               {"id": "meeting", "title": "本周会议纪要", "objective": "获取本周会议纪要",
                "tools": ["memory_search"], "agents": [], "skills": [], "output": "纪要摘要"},
               {"id": "progress", "title": "AIGC进度总结", "objective": "获取本周AIGC进度",
                "tools": ["memory_search"], "agents": [], "skills": [], "output": "进度摘要"},
               {"id": "metrics", "title": "GPU算力数据", "objective": "获取本周GPU平台数据",
                "tools": ["data_search"], "agents": [], "skills": [], "output": "原值表"},
               {"id": "tokens", "title": "Token数据", "objective": "获取本周Token看板数据",
                "tools": ["data_search"], "agents": [], "skills": [], "output": "Token表"},
           ]}
SUPPORT = {"id": "style", "title": "周报表达规则", "workflowRole": "support",
           "writingDesign": "先给结论与证据"}
FINGERPRINT = "a" * 64


class ReviewService(DeliveryService):
    async def _stream_direct_completion(self, **_kwargs):
        raise AssertionError("独立审查应由定向 fake 实现，不得调用真实模型")
        yield ""


def binding(*, source="automatic", support=True):
    skills = [{"id": "weekly", "role": "primary", "selection_source": source}]
    if support:
        skills.append({"id": "style", "role": "support", "selection_source": "dependency"})
    return {"version": 1, "fingerprint": FINGERPRINT, "skills": skills}


def state_for(loop, *, source="automatic", support=True):
    profiles = [PRIMARY, SUPPORT] if support else [PRIMARY]
    state = loop._new_state(
        user_message=CHAPTER, root_request=ROOT, current_document="", conversation=[],
        selected_skills=profiles,
        options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        model_mode="local", session_id="chapter-frozen", run_id="run-frozen",
        creation_mode="brainstorm", creation_brief={
            "root_request": ROOT, "decisions": [],
            "chapter_context": {"id": "body", "title": "创作正文", "goal": "完成周报",
                                "index": 1, "total": 1, "confirmed_decisions": [],
                                "outline": [{"id": "body", "title": "创作正文", "goal": "完成周报"}]},
        })
    explicit = ["weekly"] if source == "user" else []
    loop._bind_chapter_skill_binding(state, binding(source=source, support=support),
                                     profiles, explicit, restoring=False)
    return state


def accepted_review(skill, instruction):
    assert instruction == ROOT
    return {"skill_id": skill["id"], "status": "reviewed", "fingerprint": "reviewed",
            "requested_deliverable": "GPU成本优化周报", "actual_workflow_deliverable": "GPU成本优化周报",
            "metadata_consistent": True, "applicable": True, "request_evidence": "GPU成本优化",
            "conflicts": [], "missing_inputs": []}


def test_resumed_ranked_skill_step_uses_existing_102_row_snapshot_as_bounded_query_view():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    header = ["项目名称", "领域", "卡数", "年内成本（万元）", "年化成本（万元）", "年化收益（万元）", "ROI"]
    rows = [["项目{}".format(index), "领域", str(index), "—", "{}万元".format(index), "—", "—"]
            for index in range(1, 103)]
    source = {
        "source_id": 41, "title": "项目用量表", "source_kind": "report_url",
        "source_url": "https://example.test/project", "can_use": True,
        "collected_at": 1000, "content_excerpt": "项目1 1万元 项目2 2万元",
        "structured_data": {
            "tables": [[header, *rows[:50]], [header, *rows[50:]]],
            "pagination": {"dataset_complete": True, "pages_captured": 2, "total_rows": 102},
        },
    }
    state.environment["strict_skill_workflow"] = True
    state.environment["current_data_results"] = [source]
    state.environment["references"] = [{
        "source_id": 9, "title": "另一步骤的旧资料", "content": "UNRELATED_OLD_REFERENCE",
        "matched_skill_steps": ["meeting"],
    }]
    state.environment["data_query_results"] = [{
        "shape": "narrative", "plan": {"mode": "narrative"},
        "rows": [], "skill_step_id": "metrics",
        "validation": {"status": "not_applicable"},
    }]
    step = {
        "id": "creation_main_agent", "action": "skill_step", "skill_step_id": "metrics",
        "skill_step_title": "项目数据",
        "skill_step_objective": "按年化成本的数值降序选择前10个项目行；表格列沿用页面已有的项目名称、领域、卡数、年内成本、年化成本、年化收益、ROI。",
        "skill_step_output": "项目排名表",
    }

    loop._ensure_explicit_ranked_queries(state, step)
    loop._ensure_explicit_ranked_queries(state, step)
    results = [item for item in state.environment["data_query_results"]
               if item.get("shape") == "table"]
    assert len(results) == 1
    assert results[0]["validation"]["status"] == "verified"
    assert len(results[0]["rows"]) == 10
    assert state.environment["explicit_ranked_query_receipts"]["metrics"]["status"] == "completed"

    prompt = loop._prompt_environment(state, step)
    assert "项目102" in prompt
    assert "项目93" in prompt
    assert "项目50" not in prompt
    assert "见同源确定性数据查询结果" in prompt
    assert "narrative" not in prompt
    assert "UNRELATED_OLD_REFERENCE" not in prompt


def test_same_skill_period_and_search_query_flow_into_report_refresh(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["tool_results"] = [{
        "tool_id": "memory_search", "skill_id": "weekly", "skill_step_id": "meeting",
        "time_context": {"has_relative_time": True, "period_kind": "current_week",
                         "period_start": "2026-09-28", "period_end": "2026-10-04"},
    }]
    data_step = {
        "id": "data_search", "skill_id": "weekly", "skill_step_id": "metrics",
        "skill_step_title": "项目数据", "skill_step_objective": "查询用户要求周期的项目表",
    }
    root_requirement = {"time_context": {"has_relative_time": False,
                                         "period_kind": "", "period_start": "2026-09-28",
                                         "period_end": "2026-09-28"}}
    effective = loop._requirement_with_skill_period(state, data_step, root_requirement)
    assert effective["time_context"]["period_kind"] == "current_week"
    assert root_requirement["time_context"]["period_kind"] == ""
    state.environment["tool_results"].append({
        "tool_id": "data_search", "skill_id": "weekly", "skill_step_id": "metrics",
        "query": "当前步骤：项目数据\n查询用户要求周期的项目表",
        "time_context": effective["time_context"],
    })
    monkeypatch.setattr(loop.service, "analyze_requirement",
                        lambda _query, _options: root_requirement)
    scrape_step = {**data_step, "id": "webpage_scrape"}
    query, scrape_requirement = loop._scrape_step_context(state, scrape_step)
    assert query == state.environment["tool_results"][-1]["query"]
    assert scrape_requirement["time_context"]["period_start"] == "2026-09-28"
    assert scrape_requirement["time_context"]["period_end"] == "2026-10-04"
    assert "创作正文" not in query


def test_unrelated_historical_step_does_not_inherit_weekly_period():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["tool_results"] = [{
        "tool_id": "memory_search", "skill_id": "weekly", "skill_step_id": "meeting",
        "time_context": {"has_relative_time": True, "period_kind": "current_week",
                         "period_start": "2026-09-28", "period_end": "2026-10-04"},
    }]
    requirement = {"time_context": {"has_relative_time": False, "period_kind": ""}}
    step = {"skill_id": "weekly", "skill_step_objective": "对比历史年度成本"}
    assert loop._requirement_with_skill_period(state, step, requirement) is requirement


def _non_skill_brainstorm_period_state(loop, period):
    return loop._new_state(
        user_message=CHAPTER, root_request=ROOT, current_document="", conversation=[],
        selected_skills=[], options=CreationOptions(enabled_tools=("data_search",)),
        model_mode="local", session_id="chapter-no-skill", run_id="run-no-skill",
        creation_mode="brainstorm", creation_brief={
            "root_request": ROOT, "decisions": [],
            "confirmed_report_period": period,
        })


def test_non_skill_brainstorm_data_search_uses_exact_confirmed_period():
    loop = CreationAgentLoop(ReviewService())
    state = _non_skill_brainstorm_period_state(loop, {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    })
    requirement = {"time_context": {
        "has_relative_time": False, "period_start": "2026-09-30",
        "period_end": "2026-09-30", "period_start_ms": 1790697600000,
        "period_end_ms": 1790783999999, "current_date": "2026-09-30",
    }}
    step = {"id": "data_search", "input_query": (
        "查询 2026-09-21 至 2026-09-27 期间的 GPU 账单及用量数据")}

    effective = loop._requirement_with_skill_period(state, step, requirement)
    period = effective["time_context"]
    zone = ZoneInfo("Asia/Shanghai")
    assert period["period_source"] == "confirmed_report_period"
    assert period["has_absolute_time"] is True
    assert period["has_relative_time"] is False
    assert (period["period_start"], period["period_end"], period["timezone"]) == (
        "2026-09-21", "2026-09-27", "Asia/Shanghai")
    assert period["period_start_ms"] == int(datetime(
        2026, 9, 21, tzinfo=zone).timestamp() * 1000)
    assert period["period_end_ms"] == int(datetime(
        2026, 9, 27, 23, 59, 59, 999999, tzinfo=zone).timestamp() * 1000)
    assert requirement["time_context"]["period_start"] == "2026-09-30"


def test_non_skill_brainstorm_explicit_step_period_has_priority():
    loop = CreationAgentLoop(ReviewService())
    state = _non_skill_brainstorm_period_state(loop, {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    })
    requirement = {"time_context": {"has_relative_time": False,
                                    "period_start": "2026-09-30", "period_end": "2026-09-30"}}
    step = {"id": "data_search", "input_query": (
        "查询可比期 2026-09-14 至 2026-09-20 的 GPU 账单")}

    effective = loop._requirement_with_skill_period(state, step, requirement)
    assert effective["time_context"]["period_source"] == "step_explicit_range"
    assert effective["time_context"]["period_start"] == "2026-09-14"
    assert effective["time_context"]["period_end"] == "2026-09-20"
    assert requirement["time_context"]["period_start"] == "2026-09-30"


@pytest.mark.parametrize("period", [
    {"start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"},
    {"start": "2026-09-21", "end": "2026-09-27", "timezone": "Mars/Olympus", "source": "user"},
    {"start": "2026-09-27", "end": "2026-09-21", "timezone": "Asia/Shanghai", "source": "user"},
])
def test_non_skill_brainstorm_does_not_infer_unverified_period(period):
    loop = CreationAgentLoop(ReviewService())
    state = _non_skill_brainstorm_period_state(loop, period)
    requirement = {"time_context": {"has_relative_time": False,
                                    "period_start": "2026-09-30", "period_end": "2026-09-30"}}
    step = {"id": "data_search", "input_query": "查询 GPU 账单"}
    assert loop._requirement_with_skill_period(state, step, requirement) is requirement


def test_non_skill_brainstorm_preserves_parsed_step_relative_period():
    loop = CreationAgentLoop(ReviewService())
    state = _non_skill_brainstorm_period_state(loop, {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    })
    requirement = {"time_context": {"has_relative_time": True,
                                    "period_kind": "previous_week",
                                    "period_start": "2026-09-14", "period_end": "2026-09-20"}}
    step = {"id": "data_search", "input_query": "查询上周 GPU 账单"}
    assert loop._requirement_with_skill_period(state, step, requirement) is requirement


def test_confirmed_period_overrides_relative_word_in_inherited_root_query():
    loop = CreationAgentLoop(ReviewService())
    state = _non_skill_brainstorm_period_state(loop, {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    })
    requirement = {"time_context": {"has_relative_time": True,
                                    "period_kind": "current_week",
                                    "period_start": "2026-09-28", "period_end": "2026-10-04"}}
    step = {"id": "data_search", "input_query": "查询 GPU 账单"}
    effective = loop._requirement_with_skill_period(state, step, requirement)
    assert effective["time_context"]["period_source"] == "confirmed_report_period"
    assert effective["time_context"]["period_start"] == "2026-09-21"
    assert effective["time_context"]["has_relative_time"] is False


def test_verified_ranked_row_cannot_be_described_as_outside_the_top_n():
    result = {
        "shape": "table", "validation": {"status": "verified"},
        "plan": {"operations": [{"op": "order_by", "field_id": "cost"},
                                {"op": "limit", "value": 10}]},
        "schema": [{"field_id": "name", "type": "string"},
                   {"field_id": "cost", "type": "decimal"}],
        "rows": [{"row_id": "r9", "cells": {"name": {"raw": "智能客服"},
                                          "cost": {"raw": "387万元"}}}],
        "provenance": {"relation_id": "source_41.table_0",
                       "source_url": "https://example.test/metrics"},
    }
    assert CreationAgentLoop._ranked_query_claim_conflicts(
        "智能客服虽未在Top 10中体现，但仍有较高ROI。", [result]) == [{
            "row_id": "r9", "relation_id": "source_41.table_0", "name": "智能客服",
            "limit": 10, "line_number": 1,
            "excerpt": "智能客服虽未在Top 10中体现",
        }]
    assert CreationAgentLoop._ranked_query_claim_conflicts(
        "智能客服位于年化成本前10项目之中。", [result]) == []


def test_rank_conflict_binds_negative_predicate_to_its_own_subject_clause():
    from creation.source_grounded_revision import revise_source_grounded_preview
    result = {
        "shape": "table", "validation": {"status": "verified"},
        "plan": {"operations": [{"op": "order_by", "field_id": "cost"},
                                {"op": "limit", "value": 2}]},
        "schema": [{"field_id": "name", "type": "string"}],
        "rows": [{"row_id": "r1", "cells": {"name": {"raw": "项目甲"}}},
                 {"row_id": "r2", "cells": {"name": {"raw": "项目乙"}}}],
        "provenance": {"relation_id": "source_41.table_0"},
    }
    document = "项目甲已进入Top 2，项目乙未进入Top 2。"
    conflicts = CreationAgentLoop._ranked_query_claim_conflicts(document, [result])
    assert [(item["name"], item["excerpt"]) for item in conflicts] == [
        ("项目乙", "项目乙未进入Top 2")]
    revised, edits = revise_source_grounded_preview(document, [], conflicts)
    assert len(edits) == 1
    assert "项目甲已进入Top 2" in revised
    assert "项目乙未进入Top 2" not in revised
    assert "已验证的前 2 行包含「项目乙」" in revised
    assert CreationAgentLoop._ranked_query_claim_conflicts(revised, [result]) == []
    carried = CreationAgentLoop._ranked_query_claim_conflicts(
        "项目甲成本较低，虽未进入Top 2。", [result])
    assert [item["name"] for item in carried] == ["项目甲"]
    only_first = {**result, "rows": result["rows"][:1]}
    assert CreationAgentLoop._ranked_query_claim_conflicts(
        "项目甲已进入Top 2且项目乙未进入Top 2。", [only_first]) == []
    same_clause = CreationAgentLoop._ranked_query_claim_conflicts(
        "项目甲已进入Top 2且项目乙未进入Top 2。", [result])
    assert [item["name"] for item in same_clause] == ["项目乙"]


def test_rank_conflict_requires_current_verified_table_values_and_source():
    result = {
        "shape": "table", "validation": {"status": "verified"},
        "plan": {"operations": [{"op": "order_by", "field_id": "cost"},
                                {"op": "limit", "value": 2}]},
        "schema": [{"field_id": "name", "type": "string"},
                   {"field_id": "cost", "type": "decimal"}],
        "rows": [
            {"row_id": "r1", "cells": {"name": {"raw": "项目甲"},
                                      "cost": {"raw": "100万元"}}},
            {"row_id": "r2", "cells": {"name": {"raw": "项目乙"},
                                      "cost": {"raw": "80万元"}}},
        ],
        "provenance": {"relation_id": "source_current.table_0",
                       "source_url": "https://example.test/current"},
    }
    document = (
        "## 2024 成本参考\n\n| 项目 | 成本 |\n| :--- | :--- |\n"
        "| 项目甲 | 50万元 |\n项目甲未进入Top 2。\n\n"
        "## 当前成本\n\n| 项目 | 成本 |\n| :--- | :--- |\n"
        "| 项目甲 | 100万元 |\n| 项目乙 | 80万元 |\n"
        "来源：https://example.test/current\n"
        "项目甲未进入Top 2。")
    conflicts = CreationAgentLoop._ranked_query_claim_conflicts(document, [result])
    assert [(item["name"], item["line_number"]) for item in conflicts] == [
        ("项目甲", len(document.splitlines()))]
    only_old = document.split("## 当前成本")[0]
    assert CreationAgentLoop._ranked_query_claim_conflicts(only_old, [result]) == []
    wrong_value = document.replace("| 项目甲 | 100万元 |", "| 项目甲 | 50万元 |")
    assert CreationAgentLoop._ranked_query_claim_conflicts(wrong_value, [result]) == []
    without_source = document.replace("https://example.test/current", "未注明来源")
    assert CreationAgentLoop._ranked_query_claim_conflicts(without_source, [result]) == []


@pytest.mark.asyncio
async def test_frozen_primary_and_support_override_optional_model_generation(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        assert "创作正文" in _kwargs["confirmed_brief"]
        return accepted_review(skill, instruction)
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    events = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert state.environment["operation"]["kind"] == "execute_skill"
    assert state.environment["operation"]["skill_ids"] == ["weekly"]
    assert state.environment["operation"]["constraint_skill_ids"] == ["style"]
    assert [(item["skill_id"], item["role"], item["source"]) for item in
            state.environment["skill_admission"]] == [
                ("weekly", "primary", "automatic"), ("style", "support", "dependency")]
    assert state.environment["strict_skill_workflow"] is True
    assert [step["skill_step_id"] for step in state.plan
            if step.get("skill_step_id") and step.get("kind") == "tool"] == [
        "meeting", "progress", "metrics", "tokens"]
    assert {item["title"] for item in loop._declared_workflow_requirements(
        state, state.environment["routing_decision"])} == {
            "本周会议纪要", "AIGC进度总结", "GPU算力数据", "Token数据"}
    assert any(event["type"] == "skill.admission" for event in events)


@pytest.mark.asyncio
async def test_frozen_user_primary_keeps_source_without_reinterpreting_chapter_instruction(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        return accepted_review(skill, instruction)
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, source="user", support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert state.environment["explicit_skill_ids"] == ["weekly"]
    assert state.environment["skill_admission"][0]["source"] == "user"
    assert state.environment["routed_skill_ids"] == ["weekly"]


@pytest.mark.asyncio
@pytest.mark.parametrize("review_change,code", [
    ({"status": "unreviewed"}, "CREATION_SKILL_REVIEW_UNAVAILABLE"),
    ({"applicable": False, "conflicts": ["交付物范围不一致"]}, "CREATION_SKILL_NOT_APPLICABLE"),
    ({"missing_inputs": ["必须由用户提供审批凭证"]}, "CREATION_SKILL_INPUTS_MISSING"),
])
async def test_frozen_skill_review_failure_never_downgrades_to_generic_generation(
        monkeypatch, review_change, code):
    async def review(_service, skill, instruction, **_kwargs):
        return {**accepted_review(skill, instruction), **review_change}
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    with pytest.raises(OperationError) as exc:
        _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert exc.value.code == code
    assert "routing_decision" not in state.environment


def test_resume_rejects_old_or_changed_binding_before_reusing_checkpoint_plan():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop)
    frozen = copy.deepcopy(state.environment["brainstorm_skill_binding"])
    restored = type(state).restore(state.serializable())
    loop._bind_chapter_skill_binding(restored, frozen, [PRIMARY, SUPPORT], [], restoring=True)
    assert [item["id"] for item in restored.selected_skills] == ["weekly", "style"]
    del restored.environment["brainstorm_skill_binding"]
    with pytest.raises(OperationError) as exc:
        loop._bind_chapter_skill_binding(restored, frozen, [PRIMARY, SUPPORT], [], restoring=True)
    assert exc.value.code == "CREATION_SKILL_BINDING_CHANGED"
    restored.environment["brainstorm_skill_binding"] = frozen
    changed = {**frozen, "fingerprint": "b" * 64}
    with pytest.raises(OperationError) as exc:
        loop._bind_chapter_skill_binding(restored, changed, [PRIMARY, SUPPORT], [], restoring=True)
    assert exc.value.code == "CREATION_SKILL_BINDING_CHANGED"


@pytest.mark.asyncio
async def test_support_review_checks_its_role_against_the_frozen_primary():
    class Model:
        calls = []

        async def _stream_direct_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield json.dumps({
                "requested_deliverable": "GPU成本优化周报",
                "actual_workflow_deliverable": "辅助图表",
                "metadata_consistent": True, "applicable": True,
                "request_evidence": "GPU成本优化", "conflicts": [], "missing_inputs": [],
            }, ensure_ascii=False)

    service = Model()
    review = await skill_governance.review_skill(
        service, SUPPORT, ROOT, role="support", primary_skills=[PRIMARY])
    assert review["status"] == "reviewed"
    assert "辅助角色" in service.calls[0]["system_prompt"]
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["role"] == "support"
    assert [item["id"] for item in payload["primary_skills"]] == ["weekly"]
    assert payload["primary_skills"][0]["steps"][0]["tools"] == ["memory_search"]


@pytest.mark.asyncio
async def test_frozen_retrieval_workflow_review_includes_standard_document_writer():
    class Model:
        calls = []

        async def _stream_direct_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield json.dumps({
                "requested_deliverable": "周报", "actual_workflow_deliverable": "周报",
                "metadata_consistent": True, "applicable": True,
                "request_evidence": "GPU成本优化", "conflicts": [], "missing_inputs": [],
            }, ensure_ascii=False)

    service = Model()
    result = await skill_governance.review_skill(
        service, PRIMARY, ROOT, frozen=True, confirmed_brief="已确认写作决策")
    assert result["status"] == "reviewed"
    assert "标准创作 Agent 写作" in service.calls[0]["system_prompt"]
    assert "后续交付完整性" in service.calls[0]["system_prompt"]
    payload = json.loads(service.calls[0]["user_prompt"])
    assert payload["frozen_chapter_binding"] is True
    assert payload["confirmed_brief"] == "已确认写作决策"
    assert payload["skill"]["steps"][2]["tools"] == ["data_search"]
    assert payload["frozen_chapter_workflow"]["runtime_steps"] == [
        {"agent_id": "creation_main_agent", "action": "skill_step",
         "name": "逐项撰写正文", "output": "按已确认简报撰写每个技能步骤的正文内容"},
        {"actor": "creation_main_agent", "operation": "strict_skill_workflow_assembly",
         "name": "组装完整文档", "output": "按所选技能步骤和章节边界组装完整目标文档"},
        {"agent_id": "delivery_validation", "action": "delivery_check",
         "name": "交付核验", "output": "核验交付条件并修正未通过的正文"},
    ]
    assert "与 skill.steps 合并评估" in service.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_frozen_runtime_writer_does_not_override_incompatible_skill_verdict():
    class Model:
        async def _stream_direct_completion(self, **kwargs):
            assert "frozen_chapter_workflow" in json.loads(kwargs["user_prompt"])
            yield json.dumps({
                "requested_deliverable": "GPU成本优化周报",
                "actual_workflow_deliverable": "采购审批申请",
                "metadata_consistent": True, "applicable": False,
                "request_evidence": "GPU成本优化",
                "conflicts": ["技能强制交付采购审批申请，与本次周报不一致"],
                "missing_inputs": [],
            }, ensure_ascii=False)

    review = await skill_governance.review_skill(
        Model(), {"id": "procurement", "title": "采购审批申请",
                  "executionSteps": [{"id": "request", "title": "审批申请",
                                      "objective": "生成采购审批申请", "tools": []}]},
        ROOT, frozen=True)
    assert review["status"] == "reviewed"
    assert review["applicable"] is False
    assert review["conflicts"] == ["技能强制交付采购审批申请，与本次周报不一致"]


def test_frozen_skill_fit_brief_keeps_confirmed_choices_without_historical_prose():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    brief = state.environment["creation_brief"]
    brief["brief_markdown"] = "历史周报说资料已齐全"
    brief["brief_edits"] = {"root_request": "请生成 GPU 成本优化周报"}
    brief["decisions"] = [
        {"dimension": "读者", "summary": "管理决策者", "source": "user"},
        {"dimension": "资料", "summary": "历史值已确认", "source": "agent_assumption"},
        {"dimension": "范围", "summary": "排除旧年度", "source": "user_excluded"},
    ]
    context = json.loads(loop._skill_relevance_brief_context(state))
    assert context["root_request"] == ROOT
    assert context["brief_edits"] == brief["brief_edits"]
    assert [item["summary"] for item in context["confirmed_decisions"]] == [
        "管理决策者", "排除旧年度"]
    assert "历史周报说资料已齐全" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["", "GPU成本优化周报"])
async def test_positive_frozen_review_missing_quote_uses_literal_root_not_skill_title(monkeypatch, quote):
    async def review(_service, skill, instruction, **_kwargs):
        return {**accepted_review(skill, instruction), "request_evidence": quote}
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert state.environment["skill_admission"][0]["admitted"] is True
    assert state.environment["skill_admission"][0]["source"] == "automatic"


@pytest.mark.asyncio
async def test_frozen_incompatible_workflow_still_rejects_concrete_conflict(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        return {**accepted_review(skill, instruction), "metadata_consistent": True,
                "applicable": False, "request_evidence": "",
                "conflicts": ["步骤强制产出技术架构设计文档，与周报交付物冲突"]}
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    with pytest.raises(OperationError) as exc:
        _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert exc.value.code == "CREATION_SKILL_NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_retrieval_addressable_review_gap_runs_workflow_and_keeps_pending_receipt(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        return {**accepted_review(skill, instruction),
                "missing_inputs": ["本周会议纪要"]}
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    assert state.environment["strict_skill_workflow"] is True
    assert state.environment["brainstorm_skill_reviews"][0]["pending_retrieval_inputs"] == ["本周会议纪要"]
    assert state.environment["skill_admission"][0]["admitted"] is True


@pytest.mark.asyncio
async def test_frozen_delivery_guard_needs_distinct_step_receipts_before_pass(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        return accepted_review(skill, instruction)
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    decision = {"source": "model", "tools": [], "agents": [], "operation": {
        "kind": "generate", "document_identity": {
            "title": "GPU成本优化周报", "source": "generated", "evidence": ROOT}}}
    _ = [event async for event in loop._apply_routing_decision(state, state.plan[0], decision)]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": step_id, "content": "已写"}
        for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["tool_results"] = [
        {"tool_id": "memory_search", "skill_id": "weekly", "skill_step_id": "meeting",
         "status": "completed", "result_count": 1},
        {"tool_id": "data_search", "skill_id": "weekly", "skill_step_id": "metrics",
         "status": "completed", "result_count": 1, "usable_result_count": 1,
         "source_ids": ["shared-source"]},
    ]
    async def incorrect_model_pass(*_args):
        return {"status": "pass", "checks": [], "corrections": []}
    monkeypatch.setattr(loop.service, "review_creation_delivery", incorrect_model_pass)
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "blocked"
    assert {item["step_id"] for item in state.environment["frozen_skill_step_receipts"]
            if item["status"] == "missing_input"} == {"progress", "tokens"}
    assert {item["step_id"] for item in state.environment["frozen_skill_step_receipts"]
            if item["status"] == "completed"} == {"meeting", "metrics"}
    state.environment["tool_results"].extend([
        {"tool_id": "memory_search", "skill_id": "weekly", "skill_step_id": "progress",
         "status": "completed", "result_count": 0},
        {"tool_id": "data_search", "skill_id": "weekly", "skill_step_id": "tokens",
         "status": "completed", "result_count": 1, "usable_result_count": 0,
         "source_ids": ["shared-source"]},
    ])
    state.environment["data_results"] = [{"source_id": "shared-source", "can_use": True}]
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "blocked"
    assert "未取得资料" in json.dumps(report, ensure_ascii=False)
    assert "不可用于本轮指标" in json.dumps(report, ensure_ascii=False)
    state.environment["tool_results"][-2]["result_count"] = 1
    state.environment["tool_results"][1]["usable_result_count"] = 0
    state.environment["tool_results"][-1]["usable_result_count"] = 1
    state.environment["data_results"] = [{"source_id": "shared-source", "can_use": False}]
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "blocked"
    assert {item["step_id"] for item in state.environment["frozen_skill_step_receipts"]
            if item["status"] == "missing_input"} == {"metrics"}
    state.environment["tool_results"][1]["usable_result_count"] = 1
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "pass"
    assert all(item["status"] == "completed" for item in state.environment["frozen_skill_step_receipts"])
    state.environment["tool_results"][0]["time_context"] = {
        "has_relative_time": True, "period_start": "2026-09-28", "period_end": "2026-10-04"}
    state.environment["tool_results"][0]["period_match_counts"] = {"unknown": 7, "outside": 3}
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "blocked"
    assert {item["step_id"] for item in state.environment["frozen_skill_step_receipts"]
            if item["status"] == "missing_input"} == {"meeting"}
    assert "目标周期内资料未确认" in json.dumps(report, ensure_ascii=False)
    state.environment["tool_results"][0]["period_match_counts"]["matched"] = 1
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "pass"
    state.environment["completed_skill_steps"][0].update({
        "title": "本周会议纪要", "content": "## AIGC进度总结\n\n这段只属于另一步。"})
    report = await loop._review_creation_delivery(state, "# GPU成本优化周报\n\n正文")
    assert report["status"] == "blocked"
    meeting_receipt = next(item for item in state.environment["frozen_skill_step_receipts"]
                           if item["step_id"] == "meeting")
    assert meeting_receipt["retained_chars"] == 0
    assert meeting_receipt["content_completed"] is False


@pytest.mark.asyncio
async def test_frozen_delivery_blocks_missing_deterministic_ranking_rows(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.plan = [{"skill_id": "weekly", "skill_step_id": step_id}
                  for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": step_id, "content": "已完成步骤正文"}
        for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["tool_results"] = [
        {"tool_id": tool_id, "skill_id": "weekly", "skill_step_id": step_id,
         "status": "completed", "result_count": 2, "usable_result_count": 2}
        for step_id, tool_id in (("meeting", "memory_search"),
                                 ("progress", "memory_search"),
                                 ("metrics", "data_search"),
                                 ("tokens", "data_search"))]
    state.environment["explicit_ranked_query_receipts"] = {
        "metrics": {"status": "completed", "required_count": 1, "completed_count": 1}}
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["data_query_results"] = [{
        "shape": "table", "skill_step_id": "metrics",
        "plan": {"presentation": "table", "operations": [
            {"op": "order_by", "field_id": "cost"}, {"op": "limit", "value": 10}]},
        "schema": [{"field_id": "name", "type": "string"},
                   {"field_id": "cost", "type": "decimal"}],
        "provenance": {"relation_id": "source_41.table_0"},
        "validation": {"status": "verified"},
        "rows": [
            {"row_id": "row_1", "cells": {
                "name": {"raw": "项目甲"}, "cost": {"raw": "100万元"}}},
            {"row_id": "row_2", "cells": {
                "name": {"raw": "项目乙"}, "cost": {"raw": "90万元"}}},
        ],
    }]

    async def incorrect_model_pass(*_args):
        return {"status": "pass", "checks": [], "corrections": []}
    monkeypatch.setattr(loop.service, "review_creation_delivery", incorrect_model_pass)
    report = await loop._review_creation_delivery(state, "# 周报\n\n| 项目甲 | 100万元 |")
    assert report["status"] == "blocked"
    assert "未逐行进入成稿" in json.dumps(report, ensure_ascii=False)
    report = await loop._review_creation_delivery(
        state, "# 周报\n\n| 项目甲 | 100万元 |\n| 项目乙 | 90万元 |")
    assert report["status"] == "pass"
    report = await loop._review_creation_delivery(
        state, "# 周报\n\n| 项目甲 | 100万元 |\n| 项目乙 | 90万元 |\n\n项目乙未进入Top 10。")
    assert report["status"] == "revise"
    assert "不得称其未进入前 10 行" in json.dumps(report, ensure_ascii=False)
    state.environment["explicit_ranked_query_receipts"]["metrics"]["status"] = "missing_input"
    report = await loop._review_creation_delivery(
        state, "# 周报\n\n| 项目甲 | 100万元 |\n| 项目乙 | 90万元 |")
    assert report["status"] == "blocked"
    assert "确定性查询未完成" in json.dumps(report, ensure_ascii=False)


@pytest.mark.asyncio
async def test_frozen_delivery_requests_rewrite_for_source_statistical_date_conflict(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.plan = [{"skill_id": "weekly", "skill_step_id": step_id}
                  for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": step_id, "content": "已完成步骤正文"}
        for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["tool_results"] = [
        {"tool_id": tool_id, "skill_id": "weekly", "skill_step_id": step_id,
         "status": "completed", "result_count": 2, "usable_result_count": 2}
        for step_id, tool_id in (("meeting", "memory_search"),
                                 ("progress", "memory_search"),
                                 ("metrics", "data_search"),
                                 ("tokens", "data_search"))]
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["data_results"] = [{
        "source_id": 41, "source_kind": "report_url", "title": "运营报表",
        "source_url": "https://example.test/report", "can_use": True,
        "collected_at": 1790582400000,
        "creation_evidence": {"validation_status": "verified", "validation": {
            "verified_claims": [{"label": "电商 GPU配额使用率", "value": "81.0%",
                                 "statistical_period": "2026-09-26"}]}}}]
    async def incorrect_model_pass(*_args):
        return {"status": "pass", "checks": [], "corrections": []}
    monkeypatch.setattr(loop.service, "review_creation_delivery", incorrect_model_pass)

    report = await loop._review_creation_delivery(
        state, "# 周报\n\n截至2026-09-28，电商 GPU配额使用率为81.0%。")
    assert report["status"] == "revise"
    assert report["source_time_audit"]["issues"][0]["code"] == "statistical_period_conflict"
    assert "2026-09-26" in json.dumps(report["corrections"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_chapter_delivery_revises_when_prose_conflicts_with_confirmed_period(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["creation_brief"]["confirmed_report_period"] = {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    }
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    async def model_pass(*_args):
        return {"status": "pass", "checks": [], "corrections": []}
    monkeypatch.setattr(loop.service, "review_creation_delivery", model_pass)
    monkeypatch.setattr(loop, "_frozen_skill_delivery_receipts", lambda _state: [])

    report = await loop._review_creation_delivery(state,
        "报告期（2026-09-28 至 2026-10-04）暂无周会纪要。")

    assert report["status"] == "revise"
    assert report["checks"][0]["id"].startswith("confirmed_period_")
    assert state.environment["confirmed_period_audit"]["issues"][0]["code"] == (
        "report_target_period_conflict")
    assert "2026-09-21" in report["corrections"][0]


@pytest.mark.asyncio
async def test_frozen_chapter_period_guard_runs_before_model_delivery(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["creation_brief"]["confirmed_report_period"] = {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user",
    }
    monkeypatch.setattr(loop, "_frozen_skill_delivery_receipts", lambda _state: [{
        "skill_id": "weekly", "step_id": "body", "title": "正文", "status": "completed",
        "missing_inputs": [],
    }])
    async def model_pass(*_args):
        raise AssertionError("确定性周期冲突应先返回修订")
    monkeypatch.setattr(loop.service, "review_creation_delivery", model_pass)

    report = await loop._review_creation_delivery(state,
        "GPU 统计日期 2026-09-26（非目标报告周期），只作参考。")

    assert report["status"] == "revise"
    assert report["confirmed_period_audit"]["issues"][0]["code"] == (
        "in_period_date_marked_outside")


def test_frozen_data_step_requires_each_requested_metric_in_its_target_period():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.plan = [{"skill_id": "weekly", "skill_step_id": step_id}
                  for step_id in ("meeting", "progress", "metrics", "tokens")]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": step_id, "content": "已完成步骤正文"}
        for step_id in ("meeting", "progress", "metrics", "tokens")]
    period = {"has_relative_time": True, "period_kind": "current_week",
              "period_start": "2026-09-28", "period_end": "2026-10-04"}
    state.environment["tool_results"] = [
        {"tool_id": tool_id, "skill_id": "weekly", "skill_step_id": step_id,
         "status": "completed", "result_count": 1, "usable_result_count": 1,
         "source_ids": [41] if step_id == "metrics" else [],
         "time_context": period if step_id == "metrics" else {}}
        for step_id, tool_id in (("meeting", "memory_search"),
                                 ("progress", "memory_search"),
                                 ("metrics", "data_search"),
                                 ("tokens", "data_search"))]
    source = {"source_id": 41, "source_kind": "report_url", "can_use": True,
              "creation_evidence": {"validation_status": "verified", "validation": {
                  "verified_claims": [
                      {"label": "第一项", "requested_metric": "第一项", "value": "12%",
                       "statistical_period": "2026-09-28至2026-10-04"},
                      {"label": "第二项", "requested_metric": "第二项", "value": "30%",
                       "statistical_period": "2026-09-26"},
                  ]}}}
    state.environment["data_results"] = [source]

    metrics = next(item for item in loop._frozen_skill_delivery_receipts(state)
                   if item["step_id"] == "metrics")
    assert metrics["status"] == "missing_input"
    assert "第二项" in " ".join(metrics["missing_inputs"])
    source["creation_evidence"]["validation"]["verified_claims"][1]["statistical_period"] = (
        "2026-09-28至2026-10-04")
    metrics = next(item for item in loop._frozen_skill_delivery_receipts(state)
                   if item["step_id"] == "metrics")
    assert metrics["status"] == "completed"


def _confirm_source_gaps(state):
    period = {"start": "2026-09-21", "end": "2026-09-27", "timezone": "Asia/Shanghai"}
    state.environment["creation_brief"]["confirmed_report_period"] = {
        **period, "source": "user"}
    history = []
    for need in build_input_needs(state.selected_skills):
        history.append({"question": {
            "input_clarification": {"kind": "source_confirmation", "need_ids": [need["id"]]},
            "options": [{"id": "draft_with_gap", "input_value": {
                "action": "draft_with_gap", "need_id": need["id"],
                "target_period": period}}]},
            "answer": {"source": "user", "selected_option_ids": ["draft_with_gap"]}})
    state.environment["creation_brief"]["history"] = history
    return history


def _gap_document():
    return ("# GPU成本优化周报\n\n报告周期：2026-09-21 至 2026-09-27。\n\n"
            "## 本周会议纪要\n\n目标周期会议纪要待核实；历史资料不作为本周事实。\n\n"
            "## AIGC进度总结\n\n目标周期进展待核实；历史资料不作为本周事实。\n\n"
            "## GPU算力数据\n\n目标周期 GPU 指标待核实；历史报表不作为本周数据。\n\n"
            "## Token数据\n\n目标周期 Token 指标待核实；历史报表不作为本周数据。")


@pytest.mark.asyncio
async def test_user_authorized_source_gaps_complete_provisional_delivery(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    _confirm_source_gaps(state)
    state.plan = [{"skill_id": "weekly", "skill_step_id": item["id"]}
                  for item in PRIMARY["executionSteps"]]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": item["id"],
         "content": "待核实的章节正文"}
        for item in PRIMARY["executionSteps"]]
    state.environment["tool_results"] = [
        {"tool_id": item["tools"][0], "skill_id": "weekly",
         "skill_step_id": item["id"], "status": "completed", "result_count": 0}
        for item in PRIMARY["executionSteps"]]
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}

    async def model_pass(*_args):
        return {"status": "pass", "checks": [], "corrections": []}
    monkeypatch.setattr(loop.service, "review_creation_delivery", model_pass)

    report = await loop._review_creation_delivery(state, _gap_document())
    assert report["status"] == "revise"
    assert report["checks"][0]["id"] == "provisional_redaction_audit"
    receipts = loop._frozen_skill_delivery_receipts(state)
    revised, edits = revise_acknowledged_gap_preview(
        _gap_document(), state.environment["creation_brief"], receipts,
        force=True)
    state.environment["acknowledged_gap_revision_audits"] = [{
        "output_hash": loop._document_hash(revised), "edits": edits}]
    report = await loop._review_creation_delivery(state, revised)
    assert report["status"] == "pass"
    assert all(item["status"] == "completed_with_gap"
               for item in state.environment["frozen_skill_step_receipts"])
    assert all(item["gap_need_ids"] for item in state.environment["frozen_skill_step_receipts"])

    report = await loop._review_creation_delivery(
        state, _gap_document().replace("目标周期进展待核实；历史资料不作为本周事实。",
                                       "本周进展已完成，成果显著。"))
    assert report["status"] == "revise"
    assert "AIGC进度总结" in json.dumps(report["corrections"], ensure_ascii=False)

    state.environment["failed_skill_steps"] = [{"skill_id": "weekly", "step_id": "meeting"}]
    report = await loop._review_creation_delivery(state, _gap_document())
    assert report["status"] == "blocked"
    assert "步骤执行失败" in json.dumps(report["checks"], ensure_ascii=False)


def test_source_gap_choice_requires_exact_need_period_and_user_answer():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    history = _confirm_source_gaps(state)
    assert len(authorized_gap_steps(state.environment["creation_brief"], state.selected_skills)) == 4
    history[0]["answer"]["source"] = "agent"
    assert ("weekly", "meeting") not in authorized_gap_steps(
        state.environment["creation_brief"], state.selected_skills)
    history[0]["answer"]["source"] = "user"
    history[0]["question"]["options"][0]["input_value"]["target_period"]["end"] = "2026-09-28"
    assert ("weekly", "meeting") not in authorized_gap_steps(
        state.environment["creation_brief"], state.selected_skills)


def test_resume_rechecks_authorized_gap_before_replaying_failed_full_repair():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    _confirm_source_gaps(state)
    state.plan = [{"skill_id": "weekly", "skill_step_id": item["id"]}
                  for item in PRIMARY["executionSteps"]] + [
        {"id": "repair", "action": "polisher", "delivery_repair": True},
        {"id": "delivery", "action": "delivery_check"},
    ]
    state.cursor = 4
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": item["id"], "content": "已生成正文"}
        for item in PRIMARY["executionSteps"]]
    state.environment["tool_results"] = [
        {"tool_id": item["tools"][0], "skill_id": "weekly",
         "skill_step_id": item["id"], "status": "completed", "result_count": 0}
        for item in PRIMARY["executionSteps"]]
    state.environment["delivery_review"] = {"status": "revise"}
    state.environment["delivery_repair_count"] = 1

    loop._skip_obsolete_gap_repair_on_resume(state)

    assert state.cursor == 5
    assert state.environment["delivery_repair_count"] == 0
    assert state.environment["skipped_obsolete_gap_repairs"][0]["step_id"] == "repair"


def test_resumed_provisional_document_does_not_restore_withdrawn_step_claims():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["strict_skill_workflow"] = True
    state.environment["completed_skill_steps"] = [{
        "skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
        "content": "本周成本下降50%。"}]
    safe = "# GPU成本优化周报\n\n## 本周会议纪要\n\n目标周期数据待核实。"
    state.current_document = safe
    state.environment["document"] = safe
    state.environment["acknowledged_gap_revision_audits"] = [{
        "output_hash": loop._document_hash(safe), "edits": [{
            "kind": "acknowledged_source_gap", "step_id": "meeting"}]}]

    assert loop._reassemble_frozen_skill_resume_preview(
        state, "# 旧的失败草稿") is None
    assert state.current_document == safe


@pytest.mark.asyncio
async def test_resumed_provisional_final_recheck_completes_with_safe_document(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    _confirm_source_gaps(state)
    state.environment["strict_skill_workflow"] = True
    state.environment["document_identity"] = {"title": "GPU成本优化周报", "source": "generated"}
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["brainstorm_acceptance_version"] = 9
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": item["id"], "title": item["title"],
         "content": "本周已完成100%。"}
        for item in PRIMARY["executionSteps"]]
    state.environment["tool_results"] = [
        {"tool_id": item["tools"][0], "skill_id": "weekly",
         "skill_step_id": item["id"], "status": "completed", "result_count": 0}
        for item in PRIMARY["executionSteps"]]
    unsafe = ("# GPU成本优化周报\n\n" + "\n\n".join(
        "## {}\n\n本周已完成100%。".format(item["title"])
        for item in PRIMARY["executionSteps"]))
    receipts = loop._frozen_skill_delivery_receipts(state)
    safe, edits = revise_acknowledged_gap_preview(
        unsafe, state.environment["creation_brief"], receipts)
    assert len(edits) == 4
    state.environment["acknowledged_gap_revision_audits"] = [{
        "output_hash": loop._document_hash(safe), "edits": edits}]
    state.current_document = unsafe
    state.environment["document"] = unsafe
    state.environment["chapter_published_document"] = unsafe
    state.plan = [{"skill_id": "weekly", "skill_step_id": item["id"]}
                  for item in PRIMARY["executionSteps"]]
    state.cursor = len(state.plan)

    async def keep_acceptance(_state):
        return None
    monkeypatch.setattr(loop, "_recover_invalid_chapter_candidate", lambda _state: False)
    monkeypatch.setattr(loop, "_refresh_brainstorm_acceptance", keep_acceptance)
    monkeypatch.setattr(loop, "_seed_restored_delivery_failures", lambda _state: None)

    async def no_model_review(*_args):
        raise AssertionError("已确认缺口的草稿应走确定性验收")
    monkeypatch.setattr(loop.service, "review_creation_delivery", no_model_review)
    events = [event async for event in loop.run(
        user_message=CHAPTER, root_request=ROOT, current_document=unsafe,
        conversation=[], selected_skills=[PRIMARY],
        options=CreationOptions(enabled_tools=("memory_search", "data_search")),
        model_mode="local", session_id="chapter-frozen", run_id="resume-gap-final",
        creation_mode="brainstorm", creation_brief=state.environment["creation_brief"],
        available_skills=[PRIMARY], explicit_skill_ids=[],
        brainstorm_skill_binding=binding(support=False),
        resume_checkpoint=state.serializable(),
    )]
    assert events[-1]["type"] == "run.completed"
    document = events[-1]["data"]["document"]
    assert document.count("本节状态：待核实") == 4
    assert "本周已完成100%" not in document
    assert any(event["type"] == "delivery.checked" and
               event["data"]["review"].get("provisional") for event in events)


@pytest.mark.asyncio
async def test_failed_chapter_retry_replaces_draft_and_redacts_unverified_values(monkeypatch):
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    _confirm_source_gaps(state)
    prior = ("# GPU成本优化周报\n\n## GPU算力数据\n\n"
             "本次统计周期为2026-09-28至2026-10-04。\n")
    addition = ("\n\n## 创作正文\n\n## 本周会议纪要\n\n本周超2650张卡。\n\n"
                "## AIGC进度总结\n\n本周已完成100%。\n\n"
                "## GPU算力数据\n\n本次统计周期为第40周。\n\n"
                "| 项目 | 年化成本 |\n| :--- | :--- |\n| 项目甲 | 100万元 |\n\n"
                "## Token数据\n\n| 类型 | 消耗 |\n| :--- | :--- |\n"
                "| 输入 | 100亿 |")
    state.environment["chapter_base_document"] = prior
    state.environment["input_base_document"] = prior
    state.environment["operation_context"] = {
        "adopted_failed_candidate": True, "original_document": ""}
    state.environment["strict_skill_workflow"] = True
    superseded = loop._supersede_adopted_failed_chapter(state, prior + addition)
    assert superseded is not None
    assert superseded["content"].count("## GPU算力数据") == 1
    assert state.environment["chapter_base_document"] == ""
    assert state.environment["superseded_skill_previews"][-1]["document"] == prior + addition

    receipts = [{"skill_id": "weekly", "step_id": item["id"], "title": item["title"],
                 "acknowledged_gaps": ["目标周期内资料未确认"],
                 "gap_need_labels": [item["title"]]}
                for item in PRIMARY["executionSteps"]]
    revised, edits = revise_acknowledged_gap_preview(
        superseded["content"], state.environment["creation_brief"], receipts)
    assert len(edits) == 4
    assert "2026-09-21 至 2026-09-27" in revised
    assert "2650" not in revised and "100万元" not in revised and "第40周" not in revised
    assert "| 项目 | 年化成本 |" in revised and "| 类型 | 消耗 |" in revised
    assert revised.count("本节状态：待核实") == 4

    state.plan = [{"skill_id": "weekly", "skill_step_id": item["id"]}
                  for item in PRIMARY["executionSteps"]]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": item["id"], "content": "已执行步骤"}
        for item in PRIMARY["executionSteps"]]
    state.environment["tool_results"] = [
        {"tool_id": item["tools"][0], "skill_id": "weekly",
         "skill_step_id": item["id"], "status": "completed", "result_count": 0}
        for item in PRIMARY["executionSteps"]]
    state.environment["data_query_results"] = [{
        "shape": "table", "skill_step_id": "metrics",
        "validation": {"status": "verified"}, "rows": [{"row_id": "one", "cells": {
            "项目": {"raw": "项目甲"}, "成本": {"raw": "100万元"}}}],
        "provenance": {"relation_id": "historical.table"},
    }]
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["acknowledged_gap_revision_audits"] = [{
        "output_hash": loop._document_hash(revised), "edits": edits}]
    async def model_pass(*_args):
        raise AssertionError("完整资料模型审查不适用于安全撤回后的带缺口稿")
    monkeypatch.setattr(loop.service, "review_creation_delivery", model_pass)
    report = await loop._review_creation_delivery(state, revised)
    assert report["status"] == "pass"
    assert report["provisional"] is True
    assert report["source_validation"] == "pending"
    assert state.environment["provisional_query_rows_not_required"] == ["metrics"]


@pytest.mark.asyncio
async def test_frozen_chapter_keeps_existing_document_title_and_completed_prefix(monkeypatch):
    async def review(_service, skill, instruction, **_kwargs):
        return accepted_review(skill, instruction)
    monkeypatch.setattr(skill_governance, "review_skill", review)
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    base = "# 原有文档标题\n\n## 已完成章节\n\n这一章已经确认。"
    state.current_document = base
    state.environment["chapter_base_document"] = base
    state.environment["creation_brief"]["chapter_context"] = {
        "id": "body", "title": "创作正文", "goal": "完成周报", "index": 2, "total": 2,
        "confirmed_decisions": [], "outline": [
            {"id": "first", "title": "已完成章节", "goal": "既有内容"},
            {"id": "body", "title": "创作正文", "goal": "完成周报"}]}
    operation = {"kind": "generate", "document_identity": {
        "title": "模型猜的标题", "source": "generated", "evidence_hash": "0" * 64}}
    _ = [event async for event in loop._apply_frozen_chapter_skills(
        state, operation, binding(support=False), ROOT)]
    assert operation["document_identity"]["title"] == "原有文档标题"
    assert operation["document_identity"]["source"] == "existing"
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
         "content": "# 创作正文\n\n已核对会议来源。", "output_role": "section"},
        {"skill_id": "weekly", "step_id": "progress", "title": "AIGC进度总结",
         "content": "# 创作正文\n\n已核对进展来源。", "output_role": "section"}]
    state.environment["strict_skill_ids"] = ["weekly"]
    result = loop._assemble_strict_skill_document(state)
    assert result.startswith(base)
    assert result.count("## 创作正文") == 1
    assert "已核对会议来源" in result
    assert "已核对进展来源" in result
    from creation.brainstorm_chapters import validate_chapter_document
    validate_chapter_document(result, base, state.environment["creation_brief"]["chapter_context"])


def test_strict_skill_assembly_strips_paraphrased_other_step_sections():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["document_identity"] = {"title": "GPU成本优化周报", "source": "generated"}
    state.environment["strict_skill_ids"] = ["weekly"]
    state.environment["applied_skills"] = [{"id": "weekly", "execution_steps": PRIMARY["executionSteps"]}]
    state.environment["completed_skill_steps"] = [{
        "skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
        "content": ("# 创作正文\n\n## 本周会议纪要摘要\n\n会议来源待核。\n\n"
                    "## AIGC共建项目进展（推理性能优化）\n\n未执行的进展。\n\n"
                    "## GPU算力数据概览与关键指标表\n\n未执行的GPU指标。\n\n"
                    "## Token消耗数据补充项\n\n未执行的Token指标。"),
        "output_role": "section"}]
    document = loop._assemble_strict_skill_document(state)
    assert "会议来源待核" in document
    assert "创作正文" not in document
    assert "未执行的进展" not in document
    assert "未执行的GPU指标" not in document
    assert "未执行的Token指标" not in document


def test_strict_skill_assembly_skips_foreign_heading_descendants_and_own_wrapper():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["document_identity"] = {"title": "GPU成本优化周报", "source": "generated"}
    state.environment["strict_skill_ids"] = ["weekly"]
    state.environment["applied_skills"] = [{"id": "weekly", "execution_steps": PRIMARY["executionSteps"]}]
    state.environment["completed_skill_steps"] = [{
        "skill_id": "weekly", "step_id": "tokens", "title": "Token数据",
        "content": ("# 创作正文\n\n## 三、GPU算力数据\n\n"
                    "### （一）基础指标概览\n\n不应保留的 GPU 子节。\n\n"
                    "### （二）项目用量分析\n\n不应保留的项目子节。\n\n"
                    "## 四、Token数据专项分析\n\n"
                    "### 部署类型消耗对比\n\n本步骤可用的 Token 事实。"),
        "output_role": "section"}]
    document = loop._assemble_strict_skill_document(state)
    assert document.count("## Token数据") == 1
    assert "Token数据专项分析" not in document
    assert "不应保留的 GPU 子节" not in document
    assert "不应保留的项目子节" not in document
    assert "部署类型消耗对比" in document
    assert "本步骤可用的 Token 事实" in document


@pytest.mark.asyncio
async def test_failed_checkpoint_reassembles_preview_before_delivery_and_archives_old_text():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["document_identity"] = {"title": "GPU成本优化周报", "source": "generated"}
    state.environment["strict_skill_workflow"] = True
    state.environment["strict_skill_ids"] = ["weekly"]
    state.environment["applied_skills"] = [{"id": "weekly", "execution_steps": PRIMARY["executionSteps"]}]
    state.environment["completed_skill_steps"] = [
        {"skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
         "content": "## 本周会议纪要\n\n会议来源待核。", "output_role": "section"},
        {"skill_id": "weekly", "step_id": "tokens", "title": "Token数据",
         "content": ("## GPU算力数据\n\n旧的跨步骤指标。\n\n"
                     "## Token数据\n\nToken页面统计期待核。"), "output_role": "section"},
    ]
    stale = ("# GPU成本优化周报\n\n## 本周会议纪要\n\n会议来源待核。\n\n"
             "## Token数据\n\n## GPU算力数据\n\n旧的跨步骤指标。\n\n"
             "## Token数据\n\nToken页面统计期待核。")
    state.current_document = stale
    state.environment["document"] = stale
    state.environment["chapter_base_document"] = ""
    state.environment["input_base_document"] = ""
    state.environment["chapter_published_document"] = stale
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["brainstorm_acceptance_version"] = 9
    state.plan = [{"kind": "agent", "id": "delivery_validation", "name": "核对本轮交付条件",
                   "action": "delivery_check"}]
    state.cursor = 0
    events = []
    with pytest.raises(OperationError) as exc:
        async for event in loop.run(
            user_message=CHAPTER, root_request=ROOT, current_document="",
            conversation=[], selected_skills=[PRIMARY],
            options=CreationOptions(enabled_tools=("memory_search", "data_search")),
            model_mode="local", session_id="chapter-frozen", run_id="resume-frozen",
            creation_mode="brainstorm", creation_brief=state.environment["creation_brief"],
            available_skills=[PRIMARY], explicit_skill_ids=[],
            brainstorm_skill_binding=binding(support=False),
            resume_checkpoint=state.serializable(),
        ):
            events.append(event)
    assert exc.value.code == "CREATION_DELIVERY_INCOMPLETE"
    replaced = next(event for event in events if event["type"] == "document.replaced")
    assert replaced["data"]["operation"] == "strict_skill_resume_reassembly"
    assert "旧的跨步骤指标" not in replaced["data"]["content"]
    assert replaced["data"]["content"].count("## Token数据") == 1
    checkpoint = next(event["data"]["checkpoint"] for event in events
                      if event["type"] == "operation.checkpoint")
    assert checkpoint["current_document"] == replaced["data"]["content"]
    assert checkpoint["environment"]["document"] == replaced["data"]["content"]
    assert checkpoint["environment"]["chapter_published_document"] == replaced["data"]["content"]
    assert checkpoint["environment"]["superseded_skill_previews"][-1]["document"] == stale
    assert any(event["type"] == "delivery.checked" for event in events)


def test_resume_reassembly_preserves_protected_previous_body_and_rejects_changed_base():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    base = "# 用户已确认文档\n\n## 已完成章节\n\n这一段必须原样保留。"
    state.environment["creation_brief"]["chapter_context"] = {
        "id": "body", "title": "创作正文", "goal": "完成周报", "index": 2,
        "total": 2, "confirmed_decisions": [],
        "outline": [{"id": "first", "title": "已完成章节", "goal": "既有内容"},
                    {"id": "body", "title": "创作正文", "goal": "完成周报"}],
    }
    state.environment["document_identity"] = {"title": "用户已确认文档", "source": "existing"}
    state.environment["strict_skill_workflow"] = True
    state.environment["strict_skill_ids"] = ["weekly"]
    state.environment["applied_skills"] = [{"id": "weekly", "execution_steps": PRIMARY["executionSteps"]}]
    state.environment["completed_skill_steps"] = [{
        "skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
        "content": "## 本周会议纪要\n\n本章待核实内容。", "output_role": "section"}]
    old = base + "\n\n## 创作正文\n\n## 本周会议纪要\n\n本章待核实内容。\n\n## 未声明栏目\n\n旧内容。"
    state.current_document = old
    state.environment.update(document=old, chapter_published_document=old,
                             chapter_base_document=base, input_base_document=base)
    result = loop._reassemble_frozen_skill_resume_preview(state, base)
    assert result is not None
    assert result["content"].startswith(base)
    assert "旧内容" not in result["content"]
    assert state.environment["superseded_skill_previews"][-1]["document"] == old
    assert loop._reassemble_frozen_skill_resume_preview(state, base) is None
    assert len(state.environment["superseded_skill_previews"]) == 1

    changed = state_for(loop, support=False)
    changed.environment.update(copy.deepcopy(state.environment))
    changed.current_document = old
    changed.environment["document"] = old
    changed.environment["chapter_published_document"] = old
    with pytest.raises(OperationError) as exc:
        loop._reassemble_frozen_skill_resume_preview(changed, base + "\n用户新修订")
    assert exc.value.code == "CREATION_BASE_CHANGED"
    assert changed.current_document == old


@pytest.mark.asyncio
async def test_delivery_revises_source_conflicts_before_blocking_missing_period_data():
    loop = CreationAgentLoop(ReviewService())
    state = state_for(loop, support=False)
    state.environment["strict_skill_workflow"] = True
    state.environment["strict_skill_ids"] = ["weekly"]
    state.environment["document_identity"] = {"title": "GPU成本优化周报", "source": "generated"}
    state.environment["operation"] = {"kind": "execute_skill", "skill_ids": ["weekly"]}
    state.environment["chapter_base_document"] = ""
    state.environment["input_contract"] = {"inputs": [], "acceptance": []}
    state.environment["data_results"] = [{
        "source_id": 41, "source_kind": "report_url", "title": "资源报表",
        "source_url": "https://example.test/metrics", "can_use": True,
        "collected_at": 1790582400000,
        "creation_evidence": {"validation_status": "verified", "validation": {
            "verified_claims": [{"label": "资源使用率", "value": "81.0%",
                                 "statistical_period": "2026-09-26"}]}}}]
    state.environment["data_query_results"] = [{
        "shape": "table", "validation": {"status": "verified"},
        "plan": {"operations": [{"op": "order_by", "field_id": "cost"},
                                {"op": "limit", "value": 2}]},
        "schema": [{"field_id": "name", "type": "string"},
                   {"field_id": "cost", "type": "decimal"}],
        "rows": [{"row_id": "r1", "cells": {
            "name": {"raw": "项目甲"}, "cost": {"raw": "100万元"}}}],
        "provenance": {"relation_id": "source_41.table_0",
                       "source_url": "https://example.test/metrics"},
    }]
    original = ("# GPU成本优化周报\n\n## GPU算力数据\n\n"
                "截至2026-09-28，资源使用率为81.0%。\n\n"
                "| 项目 | 年化成本 |\n| :--- | :--- |\n| 项目甲 | 100万元 |\n\n"
                "来源: https://example.test/metrics\n\n"
                "项目甲虽未进入Top 2，但值得关注。")
    state.current_document = original
    state.environment["document"] = original
    state.environment["chapter_published_document"] = original
    events = []
    with pytest.raises(OperationError) as exc:
        async for event in loop._execute_step(
            state, {"kind": "agent", "id": "delivery_validation",
                    "name": "核对本轮交付条件", "action": "delivery_check"},
            creation_model=None, creation_api_key=None, creation_base_url=None,
        ):
            events.append(event)
    assert exc.value.code == "CREATION_DELIVERY_INCOMPLETE"
    replaced = next(event for event in events if event["type"] == "document.replaced")
    assert replaced["data"]["operation"] == "source_grounded_revision"
    assert "来源统计期为 2026-09-26" in replaced["data"]["content"]
    assert "已验证的前 2 行包含「项目甲」" in replaced["data"]["content"]
    assert next(event for event in events if event["type"] == "operation.checkpoint")[
        "data"]["checkpoint"]["current_document"] == replaced["data"]["content"]
    assert state.environment["superseded_skill_previews"][-1]["document"] == original
    assert state.environment["delivery_review"]["status"] == "blocked"
    assert any("未完成检索" in check["reason"]
               for check in state.environment["delivery_review"]["checks"])
    assert state.environment["source_time_audit"]["issues"] == []
    assert state.environment["ranked_query_claim_conflicts"] == []


def test_gap_redaction_retracts_unverified_chapter_introduction_and_keeps_base():
    base = "# 已完成文档\n\n## 上一章\n\n已核实的历史结论。"
    brief = {"confirmed_report_period": {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user"},
        "chapter_context": {"title": "创作正文"}}
    receipts = [{"skill_id": "weekly", "step_id": "meeting", "title": "本周会议纪要",
                 "acknowledged_gaps": ["资料未确认"], "gap_need_labels": ["周会纪要"]},
                {"skill_id": "weekly", "step_id": "gpu", "title": "GPU算力数据",
                 "acknowledged_gaps": ["指标未确认"], "gap_need_labels": ["GPU报表"]}]
    candidate = (base + "\n\n## 创作正文\n\n"
                 "本周成本下降50%，已完成 GPU 降本。\n\n"
                 "## 本周会议纪要\n\n本周完成降本。\n\n"
                 "## GPU算力数据\n\n本周成本为100万元。")

    revised, edits = revise_acknowledged_gap_preview(
        candidate, brief, receipts, protected_base=base)

    assert revised.startswith(base)
    assert "## 创作正文" in revised
    assert "本周成本下降50%" not in revised
    assert "本周成本为100万元" not in revised
    assert "已核实的历史结论" in revised
    assert revised.count("本节状态：待核实") == 2
    assert any(item["kind"] == "acknowledged_source_gap_intro" for item in edits)
    assert len([item for item in edits if item["kind"] == "acknowledged_source_gap"]) == 2


def test_gap_redaction_preserves_completed_step_before_gap_but_retracts_intro():
    brief = {"confirmed_report_period": {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "confirmed_selection"}}
    receipts = [{"skill_id": "weekly", "step_id": "meeting", "title": "已核实会议纪要",
                 "acknowledged_gaps": []},
                {"skill_id": "weekly", "step_id": "gpu", "title": "GPU算力数据",
                 "acknowledged_gaps": ["指标未确认"], "gap_need_labels": ["GPU报表"]}]
    candidate = ("# GPU成本周报\n\n本周成本下降50%。\n\n"
                 "## 已核实会议纪要\n\n9月23日会议决议已核实。\n\n"
                 "## GPU算力数据\n\n本周成本为100万元。")

    revised, edits = revise_acknowledged_gap_preview(candidate, brief, receipts)

    assert "本周成本下降50%" not in revised
    assert "9月23日会议决议已核实" in revised
    assert "本周成本为100万元" not in revised
    assert "2026-09-21 至 2026-09-27" in revised
    assert any(item["kind"] == "acknowledged_source_gap_intro" for item in edits)


def test_gap_redaction_retracts_late_claim_after_an_early_gap_disclosure():
    brief = {"confirmed_report_period": {
        "start": "2026-09-21", "end": "2026-09-27",
        "timezone": "Asia/Shanghai", "source": "user_edit"}}
    receipts = [{"skill_id": "weekly", "step_id": "gpu", "title": "GPU算力数据",
                 "acknowledged_gaps": ["指标未确认"], "gap_need_labels": ["GPU报表"]}]
    candidate = ("# GPU成本周报\n\n## GPU算力数据\n\n"
                 "目标周期数据待核实；历史资料仅作参考。\n\n"
                 + "背景说明。" * 220 + "本周 GPU 成本降低50%。")

    revised, edits = revise_acknowledged_gap_preview(candidate, brief, receipts)

    assert "本周 GPU 成本降低50%" not in revised
    assert "本节状态：待核实" in revised
    assert len([item for item in edits if item["kind"] == "acknowledged_source_gap"]) == 1
