"""Planning and repair must preserve authority before durable work exists."""

import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import (
    candidate, check_result, fixed_decision, node, prepare, setup,
)
from tests.test_brainstorm_stages import plan
from tests.brainstorm_axis_fixtures import axis_plan_manifest, axis_plan_raw


def manifest_checks(value, code="pass"):
    checks = {}
    for index, topic in enumerate(value["topics"]):
        checks["topic{}_goal".format(index)] = code
        if value["chapters"]:
            checks["topic{}_content".format(index)] = "pass"
    checks.update({"flag{}".format(index): "pass" for index in range(len(value["open_flags"]))})
    return {"checks": checks}


@pytest.mark.asyncio
async def test_writing_body_container_still_rejects_a_confirmed_content_conflict():
    value = BrainstormStageCoordinator._atomic_writing_plan({})
    decision = fixed_decision(question="故事须覆盖什么？", answer="全部人物和事件")
    rejected = manifest_checks(value)
    rejected["checks"]["topic0_content"] = "confirmed_decision_reopened:decision_1"
    coordinator, service = setup([rejected], check_consistency=True)
    before = copy.deepcopy((value, decision))
    with pytest.raises(_StageConsistencyError) as error:
        await prepare(coordinator, stage="writing", kind="plan", decisions=[decision])
    assert error.value.code == "confirmed_decision_reopened"
    assert error.value.decision_ids == ["decision_1"] and len(service.model_calls) == 1
    assert (value, decision) == before
    assert [call["num_predict"] for call in service.model_calls] == [1200]
    context = json.loads(service.prompts[0])
    units = context["validation_task"]["units"]
    assert [item["kind"] for item in units] == ["stage_topic", "chapter_content"]
    assert context["question_contexts"]["topic0"]["chapter_content"] == value["chapters"][0]
    assert context["authority"]["fixed_decisions"][0]["answer"] == decision["answer"]


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_manifest_gate_covers_every_goal_and_fails_closed_when_a_unit_is_missing(stage):
    responses = [{"checks": {}}] if stage == "writing" else [axis_plan_raw(stage), {"checks": {}}] * BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS
    coordinator, service = setup(responses,
        check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid" if stage == "writing" else "阶段思考"):
        await prepare(coordinator, stage=stage, kind="plan", decisions=[fixed_decision()])
    assert len(service.model_calls) == (1 if stage == "writing" else 2 * BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS)
    assert all(json.loads(service.prompts[i])["authority"]["fixed_decisions"]
        for i in range(1, len(service.prompts), 2))


@pytest.mark.asyncio
async def test_writing_chapter_can_restate_fixed_content_without_reopening_its_decision():
    value = plan("writing")
    value["chapters"][0]["goal"] = "展示固定的季度收入与支出范围"
    coordinator, service = setup([manifest_checks(value)], check_consistency=True)
    fixed, cleared = coordinator._stage_authority([fixed_decision()], {})
    await coordinator._check_plan_consistency(stage="writing", kind="plan", topic={},
        task={"fixed_decisions": fixed, "cleared_constraints": cleared,
            "ancestor_conditions": [], "user_constraints": {"root_request": "完整报告"}},
        result=value, other_topics=[], creation_model=None, creation_api_key=None, creation_base_url=None)
    assert len(service.model_calls) == 1
    assert "不因复述固定事实而判重开" in service.model_calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_semantic_node_repair_receives_actual_rejected_choices_and_complete_fixed_rule():
    bad = candidate(2)
    good = candidate(2)
    good["question"]["options"][0].update(label="先讲行动后讲背景", description="两部分都保留，先讲行动再补背景")
    good["question"]["options"][1].update(label="先讲背景后讲行动", description="两部分都保留，先讲背景再讲行动")
    units = ["q0_prompt", "q0_space", "q0_o0", "q0_o1"]
    bad_check = check_result("reject", "confirmed_decision_conflict", ["decision_1"], unit_ids=units)
    good_check = check_result(unit_ids=units, option_labels={"a": good["question"]["options"][0]["label"],
        "b": good["question"]["options"][1]["label"]})
    decision = fixed_decision(question="章节须包含什么？", answer="行动与背景共同保留")
    decision["selected_options"][0]["tradeoff"] = "二者都必须写入，不能改为互斥集合"
    coordinator, service = setup([bad, bad_check, good, good_check], check_consistency=True)
    original = copy.deepcopy(decision)
    result = await prepare(coordinator, node=node(2), decisions=[decision])
    feedback = json.loads(service.prompts[2])["validation_failures"][0]
    assert feedback["fixed_decisions"][0]["selected_options"][0]["tradeoff"] == decision["selected_options"][0]["tradeoff"]
    assert feedback["rejected_candidate"]["question"]["options"][0]["label"] == bad["question"]["options"][0]["label"]
    assert "details" not in json.dumps(feedback["rejected_candidate"])
    assert decision == original and result["question"]["options"][0]["label"] == good["question"]["options"][0]["label"]
    assert len(service.model_calls) == 4


def test_generation_no_longer_grants_topic_based_exception_to_reselect_fixed_period():
    prompt = BrainstormStageCoordinator._node_scope_prompt("node")
    assert "主题涉及方向、受众或周期也不授权重选已确认决定" in prompt
    assert "从不同事实切入是先后顺序" in prompt


def test_topic_root_planning_keeps_topic_boundary_while_nodes_decide_only_the_specific_goal():
    planning = BrainstormStageCoordinator._node_scope_prompt("topic_plan")
    node_prompt = BrainstormStageCoordinator._node_scope_prompt("node")
    assert "selected_topic 的完整 goal 是当前唯一主题边界" in planning
    assert "node_goal 是本次唯一要回答的具体决定" not in planning
    assert "node_goal 是本次唯一要回答的具体决定" in node_prompt
    assert "selected_topic 的完整 goal 是当前唯一主题边界" not in node_prompt
    for prompt in (planning, node_prompt):
        assert "完整 selected_skills 是当前固定写作政策" in prompt
        assert "不得询问必需内容是否保留，不能以重点为由仅保留子集" in prompt


@pytest.mark.asyncio
async def test_initial_plan_checks_explicit_user_scope_without_prior_answers():
    value = axis_plan_manifest("detail")
    rejected = manifest_checks(value, "topic_scope")
    coordinator, service = setup([axis_plan_raw("detail"), rejected] * BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS,
        check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="阶段思考"):
        await prepare(coordinator, kind="plan", root_request="只写产品内部流程，禁止扩展销售方案", decisions=[])
    context = json.loads(service.prompts[1])
    assert context["authority"]["fixed_decisions"] == []
    assert context["authority"]["user_constraints"]["root_request"] == "只写产品内部流程，禁止扩展销售方案"


@pytest.mark.asyncio
async def test_current_pending_flag_cannot_reopen_confirmed_content_when_topic_goal_is_valid():
    edits = {"open_flags": "重新确认全部已定人物范围"}
    bad = BrainstormStageCoordinator._atomic_writing_plan(edits)
    checked = manifest_checks(bad)
    checked["checks"]["flag0"] = "confirmed_decision_reopened:decision_1"
    coordinator, service = setup([checked], check_consistency=True)
    with pytest.raises(_StageConsistencyError) as error:
        await prepare(coordinator, stage="writing", kind="plan", decisions=[fixed_decision()], brief_edits=edits)
    assert error.value.code == "confirmed_decision_reopened" and error.value.decision_ids == ["decision_1"]
    units = json.loads(service.prompts[0])["validation_task"]["units"]
    assert units[-1]["kind"] == "pending_decision"
    assert units[-1]["text"] == edits["open_flags"]
    assert len(service.model_calls) == 1
