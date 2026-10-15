import json
from types import SimpleNamespace

from creation.agent_loop import CreationAgentLoop, GoalState, LoopState
from creation.operations import OperationError
import pytest


def make_brief():
    period = {"start": "2026-09-21", "end": "2026-09-27",
              "timezone": "Asia/Shanghai"}
    need = {"id": "need-bill", "label": "GPU 账单费用可核验来源"}
    stages = [
        {"id": "direction", "preparation_contract_version": 17},
        {"id": "detail", "preparation_contract_version": 17,
         "confirmed_period": {**period, "source": "user"},
         "input_preflight": {
             "period": {**period, "source": "user", "status": "confirmed"},
             "timezone": period["timezone"], "needs": [need],
         }},
        {"id": "writing", "preparation_contract_version": 17},
    ]
    history = [{"question": {
        "input_clarification": {"kind": "source_confirmation",
                                "need_ids": [need["id"]]},
        "options": [{"id": "draft_with_gap", "input_value": {
            "action": "draft_with_gap", "need_id": need["id"],
            "target_period": period}}]},
        "answer": {"source": "user", "selected_option_ids": ["draft_with_gap"]}}]
    return {"stage_workflow": {"version": 2, "stages": stages},
            "history": history}


def private_brief():
    brief = make_brief()
    detail = brief["stage_workflow"]["stages"][1]
    brief["confirmed_report_period"] = detail["confirmed_period"]
    brief["content_source_gap"] = {
        "schema_version": "brainstorm.content-source-gap.v1",
        "preparation_contract_version": 17,
        "confirmed_period": detail["confirmed_period"],
        "input_preflight": detail["input_preflight"],
    }
    del brief["stage_workflow"]
    return brief


def _state(brief):
    chapter = {"id": "chapter-1", "title": "GPU 成本优化周报",
               "goal": "核对目标周期账单与用量", "index": 1, "total": 1,
               "confirmed_decisions": []}
    brief = {**brief, "root_request": "请生成 GPU 成本优化周报",
             "decisions": [], "chapter_context": {**chapter, "outline": [
                 {key: chapter[key] for key in ("id", "title", "goal")}]}}
    return LoopState(
        session_id="isolation", run_id="run", mode="initial", model_mode="local",
        creation_mode="brainstorm", user_message="请生成 GPU 成本优化周报",
        root_request="请生成 GPU 成本优化周报", current_document="",
        conversation=[], options={}, selected_skills=[], goal=GoalState("周报"),
        environment={
            "operation": {"kind": "generate", "artifact_kind": "document"},
            "creation_brief": brief,
            "chapter_base_document": "",
            "document_identity": {"title": "GPU 成本优化周报"},
            "data_results": [{"source_id": 14840, "can_use": True,
                              "content_excerpt": "其他项目 192681ms 模型名"}],
            "references": [{"source_id": 14840, "content": "其他项目 192681ms 模型名"}],
            "data_analysis": "其他项目 192681ms 模型名",
        },
    )


def test_all_gap_agent_loop_hides_candidate_facts_and_retracts_preview():
    state = _state(private_brief())
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))

    assert loop._content_gap_active(state)
    prompt = loop._prompt_environment(state, {"id": "writer", "action": "writer"})
    assert "192681ms" not in prompt
    assert "模型名" not in prompt

    bad = "# GPU 成本优化周报\n\n其他项目耗时 192681ms 是本周 GPU 用量。"
    safe, audit = loop._apply_content_gap_candidate(state, bad)
    assert audit["status"] == "converted"
    assert safe.startswith("# 报告草稿（待核实）")
    assert "192681ms" not in safe
    assert "2026-09-21 至 2026-09-27" in safe
    assert loop._content_gap_candidate(state, safe)[0] == safe


def test_content_gap_keeps_completed_chapter_title_and_body_byte_exact():
    state = _state(private_brief())
    base = "# Original report title\n\n## Previous chapter\n\nApproved content.\n"
    state.environment["chapter_base_document"] = base
    state.environment["document_identity"]["title"] = "Different generated title"
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    candidate = base + "\n## GPU 成本优化周报\n\n其他项目耗时 192681ms。"

    safe, audit = loop._apply_content_gap_candidate(state, candidate)

    assert audit["status"] == "converted"
    assert safe.startswith(base)
    assert safe[:len(base)] == base
    assert "## GPU 成本优化周报" in safe
    assert "192681ms" not in safe
    event = loop._event(state, "document.replaced", "safe", data={"content": safe})
    assert event["data"]["content"].startswith(base)
    state.current_document = safe
    state.environment["document"] = safe
    state.environment["delivery_review"] = {"status": "pass", "provisional": True}
    state.environment["delivery_checked_hash"] = loop._document_hash(safe)
    completed = loop._event(state, "run.completed", "done", data={
        "document": safe, "references": [{"source_id": 1}],
        "evidence": [{"id": "paid-prefix-proof"}]})
    assert "references" not in completed["data"]
    assert "evidence" not in completed["data"]


def test_content_gap_filters_intermediate_document_events_before_publication():
    state = _state(private_brief())
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    bad = "# GPU 成本优化周报\n\n其他项目耗时 192681ms 是本周 GPU 用量。"
    state.current_document = bad
    state.environment["document"] = bad

    event = loop._event(state, "document.citations.validated", "citation check",
                        data={"content": bad, "audit": [{"excerpt": bad}]})
    assert "192681ms" not in event["data"]["content"]
    assert "audit" not in event["data"]
    assert "192681ms" not in state.current_document
    assert state.current_document == state.environment["document"]
    assert state.environment["superseded_content_gap_previews"][0]["document"] == bad

    preview = loop._event(state, "document.preview", "preview", data={"content": bad})
    assert preview["data"]["content"] == state.current_document
    delta = loop._event(state, "document.delta", "delta", data={"content": "192681ms"})
    assert "content" not in delta["data"]

    patch = loop._event(state, "document.patch.applied", "本周节省 15%",
                        environment_patch={"document_patch": {"summary": "本周节省 15%"}},
                        data={"content": bad, "patch": {"summary": "本周节省 15%"}})
    assert "15%" not in patch["summary"]
    assert "document_patch" not in patch["environment_patch"]
    assert "patch" not in patch["data"]
    tool = loop._event(state, "tool.completed", "检索完成",
                       environment_patch={"data_sources": [
                           {"source_id": 14840, "title": "Gemini 模型成本",
                            "can_use": False, "source_kind": "data_memory"}]})
    assert "data_sources" not in tool["environment_patch"]
    inputs = loop._event(state, "inputs.assessed", "input check", data={
        "input_contract": {"acceptance": [{"criterion": "本周已节省 15%"}]},
        "required_tools": ["data_search"]})
    assert inputs["data"] == {"required_tools": ["data_search"],
                              "source_validation": "pending"}
    agent = loop._event(state, "agent.completed", "本周已节省 15%",
                        environment_patch={"routing_decision": {"reasoning": "本周已节省 15%"}},
                        data={"execution_plan": [{"id": "writer", "action": "writer",
                            "reason": "本周已节省 15%"}]})
    assert "15%" not in str(agent)
    assert agent["data"]["execution_plan"] == [{"id": "writer", "action": "writer"}]


def test_content_gap_events_hide_unverified_source_and_reasoning_in_every_envelope():
    state = _state(private_brief())
    state.goal.objective = "本周 GPU 成本已下降 30%"
    state.goal.remaining_steps = ["引用其他项目模型账单 192681ms"]
    state.goal.outcome = "已节省 30%"
    state.environment["document_identity"]["title"] = "GPU 成本已下降 30% 的周报"
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    bad = "# GPU 成本已下降 30% 的周报\n\n其他项目模型账单 192681ms。"
    safe, _ = loop._apply_content_gap_candidate(state, bad)
    assert safe.startswith("# 报告草稿（待核实）")
    state.current_document = safe
    state.environment["document"] = safe
    state.environment["delivery_review"] = {"status": "pass", "provisional": True}
    state.environment["delivery_checked_hash"] = loop._document_hash(safe)

    events = [
        loop._event(state, "tool.completed", "本周已节省 30%",
            actor={"kind": "tool", "id": "webpage_scrape", "name": "其他项目模型账单"},
            environment_patch={"sources": [{"title": "其他项目模型账单 192681ms"}],
                "rejected_sources": [{"title": "其他项目模型账单"}],
                "data_sources": [{"title": "其他项目模型账单"}],
                "scraped_source_count": 2},
            data={"sources": [{"title": "其他项目模型账单"}],
                  "rejected_sources": [{"title": "其他项目模型账单"}],
                  "query": "其他项目模型账单", "result_count": 2}),
        loop._event(state, "intent.interpreted", "本周已节省 30%",
            data={"reasoning_summary": "其他项目模型账单 192681ms"}),
        loop._event(state, "thinking.completed", "本周已节省 30%",
            data={"stage": "routing", "reasoning": "其他项目模型账单 192681ms"}),
        loop._event(state, "phase.started", "本周已节省 30%",
            data={"phase_id": "phase-1", "phase_title": "其他项目模型账单 192681ms"}),
        loop._event(state, "document.replaced", "本周已节省 30%",
            data={"content": bad}),
    ]
    state.environment["delivery_checked_hash"] = loop._document_hash(safe)
    events.append(loop._event(state, "run.completed", "本周已节省 30%",
        data={"document": safe, "goal": {"outcome": "已节省 30%"},
              "skills": [{"title": "其他项目模型账单"}],
              "delivery_review": {"status": "pass", "provisional": True,
                                  "evidence": "其他项目模型账单 192681ms"},
              "tools": [{"tool_id": "data_search", "status": "completed",
                         "result_count": 2, "query": "其他项目模型账单"}]}))
    shown = json.dumps(events, ensure_ascii=False)
    assert "已下降 30%" not in shown
    assert "已节省 30%" not in shown
    assert "其他项目模型账单" not in shown
    assert "192681ms" not in shown
    assert events[0]["environment_patch"] == {"scraped_source_count": 2}
    assert events[0]["data"] == {"result_count": 2}
    assert events[-1]["data"]["delivery_review"] == {
        "status": "pass", "provisional": True}


def test_private_content_gap_contract_fails_closed_when_period_or_answers_disagree():
    state = _state(private_brief())
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    state.environment["creation_brief"]["content_source_gap"]["input_preflight"]["period"]["start"] = "2026-09-20"
    with pytest.raises(OperationError) as exc:
        loop._content_gap_active(state)
    assert exc.value.code == "CREATION_DOCUMENT_INVALID"


def test_private_content_gap_does_not_block_a_confirmed_source_choice():
    brief = private_brief()
    answer = brief["history"][0]
    answer["question"]["options"][0]["input_value"]["action"] = "use_selected_source"
    state = _state(brief)
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    assert not loop._saved_all_gap_intent(brief)
    assert not loop._content_gap_active(state)


def test_private_content_gap_sanitizes_events_before_operation_is_bound():
    state = _state(private_brief())
    state.environment.pop("operation")
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))
    assert loop._content_gap_active(state)
    event = loop._event(state, "inputs.assessed", "已节省 30%", data={
        "input_contract": {"acceptance": [{"criterion": "已节省 30%"}]},
        "required_tools": ["data_search"]})
    assert event["data"] == {"required_tools": ["data_search"],
                             "source_validation": "pending"}
    assert "已节省 30%" not in json.dumps(event, ensure_ascii=False)


def test_legacy_and_skill_sessions_keep_existing_prompt_evidence():
    brief = make_brief()
    brief["stage_workflow"]["stages"][0]["preparation_contract_version"] = 16
    state = _state(brief)
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))

    assert not loop._content_gap_active(state)
    prompt = loop._prompt_environment(state, {"id": "writer", "action": "writer"})
    assert "192681ms" in prompt


def test_resume_accepts_only_additive_core_source_gap_contract():
    state = _state(make_brief())
    saved = state.environment["creation_brief"]
    live = {**saved, "content_source_gap": {
        "schema_version": "brainstorm.content-source-gap.v1"}}
    CreationAgentLoop._validate_resume_context(
        state, "isolation", "", live)
    assert state.environment["creation_brief"] == live
    assert "creation_brief_context" not in state.environment
    changed = {**live, "root_request": "其他目标"}
    with pytest.raises(OperationError):
        CreationAgentLoop._validate_resume_context(state, "isolation", "", changed)


def test_old_no_skill_checkpoint_rebinds_catalog_and_retracts_raw_preview():
    state = _state(make_brief())
    state.selected_skills = [{"id": "installed-catalog", "title": "unselected"}]
    old = "# GPU 成本优化周报\n\n其他项目耗时 192681ms 是本周 GPU 用量。"
    state.current_document = old
    state.environment["document"] = old
    live = {**state.environment["creation_brief"],
            "content_source_gap": private_brief()["content_source_gap"]}
    CreationAgentLoop._validate_resume_context(state, "isolation", old, live)
    CreationAgentLoop._bind_chapter_skill_binding(
        state, None, [], [], restoring=True)
    loop = CreationAgentLoop(SimpleNamespace(_clip=lambda value, limit: str(value)[:limit]))

    assert state.selected_skills == []
    assert state.environment["unbound_skill_catalog_snapshot"][0]["id"] == "installed-catalog"
    assert loop._content_gap_active(state)
    superseded = loop._retract_restored_content_gap_candidate(state)
    assert superseded == loop._document_hash(old)
    assert "192681ms" not in state.current_document
    assert state.current_document == state.environment["document"]


def test_followup_cannot_launder_provisional_gap_as_confirmed_fact():
    brief = private_brief()
    with pytest.raises(OperationError) as error:
        CreationAgentLoop._guard_provisional_content_gap_followup(
            "brainstorm", brief)
    assert error.value.code == "CREATION_EVIDENCE_UNAVAILABLE"
    assert "现有草稿已保留" in str(error.value)

    # A new chapter still runs its own source-gap contract. A fully confirmed
    # follow-up no longer carries the provisional contract from Core.
    CreationAgentLoop._guard_provisional_content_gap_followup(
        "brainstorm", {**brief, "chapter_context": _state(brief).environment[
            "creation_brief"]["chapter_context"]})
    CreationAgentLoop._guard_provisional_content_gap_followup(
        "brainstorm", {"root_request": "周报"})


@pytest.mark.asyncio
async def test_exact_gap_draft_passes_only_provisional_delivery_with_missing_metrics():
    state = _state(private_brief())
    loop = CreationAgentLoop(SimpleNamespace())
    safe, audit = loop._apply_content_gap_candidate(state, "# Unsafe\n\n本周成本下降 15%。")
    assert audit["status"] == "converted"
    state.current_document = safe
    state.environment["document"] = safe
    state.environment["input_contract"] = {"inputs": [{
        "id": "business_data", "state": "missing", "need": "本期 GPU 账单"}]}

    report = await loop._review_creation_delivery(state, safe)
    assert report["status"] == "pass"
    assert report["provisional"] is True
    assert report["source_validation"] == "pending"
    assert report["document_hash"] == loop._document_hash(safe)
    assert report["acknowledged_gap_need_ids"] == ["need-bill"]
