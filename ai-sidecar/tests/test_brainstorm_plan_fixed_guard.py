"""Non-question plans keep full confirmed-rule gates after protocol separation."""

import copy
import json

import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import fixed_decision
from tests.test_brainstorm_stages import topic
from tests.test_creation_brainstorm import StubCreationService


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}


def authority_task():
    decisions = []
    for index in range(1, 25):
        rule = fixed_decision(question="第{}项已定约束是什么？".format(index),
            answer="保留第{}项完整既定约束".format(index))
        rule["question_id"] = "fixed_{}".format(index)
        rule["user_inputs"] = ["第{}项用户原输入".format(index)]
        decisions.append(rule)
    decisions[22].update(answer_source="user_excluded", question="营销方案是否纳入？",
        answer="禁止加入营销方案", excluded_topic="营销方案")
    edits = {"fixed_24": "检测到问题即暂停出库，不等待累计次数"}
    fixed, cleared = BrainstormStageCoordinator._stage_authority(decisions, edits)
    return {"fixed_decisions": fixed, "cleared_constraints": cleared,
        "ancestor_conditions": [], "goal": "在已定约束内规划未定表达方法",
        "user_constraints": {"root_request": "规划本期内部报告", "brief_edits": edits}}


def manifest(count=1):
    chapters = [{"id": "chapter{}".format(index), "title": "独立章{}".format(index),
        "goal": "说明第{}项本章事项，继承全部共同要求".format(index)} for index in range(count)]
    return {"topics": [{"id": "topic{}".format(index), "title": chapter["title"],
        "goal": BrainstormStageCoordinator.WRITING_AXES[0]["goal"], "chapter_id": chapter["id"]}
        for index, chapter in enumerate(chapters)], "chapters": chapters, "open_flags": []}


def inputs(kind):
    if kind == "plan":
        value = manifest()
        value["chapters"][0]["goal"] = "本章将累计两次问题才暂停出库作为固定执行规则"
        return "writing", value, {}, {"topic0_goal": "pass", "topic0_content": "pass"}, "topic0_content"
    return "detail", {"root_goals": ["细化完整检查记录的组织方法", "决定累计两次问题才暂停出库"]}, topic(), {
        "root0": "pass", "root1": "pass"}, "root1"


@pytest.mark.parametrize("kind", ["plan", "topic_plan"])
@pytest.mark.parametrize("code", ["confirmed_decision_conflict", "confirmed_decision_reopened"])
@pytest.mark.asyncio
async def test_actual_nonquestion_verifier_rejects_last_rule_conflict_even_when_previous_unit_passes(kind, code):
    stage, result, selected_topic, checks, conflicting_unit = inputs(kind)
    checks[conflicting_unit] = code + ":decision_24"
    task = authority_task()
    before = copy.deepcopy((result, selected_topic, task))
    service = StubCreationService([json.dumps({"checks": checks}, ensure_ascii=False)])
    coordinator = BrainstormStageCoordinator(service)
    # Use the real new-axis method (which returns [] for a plan), not the
    # isolated question-suite stub. Empty certificates cannot skip plan rules.
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_stage_consistency(stage=stage, kind=kind, topic=selected_topic,
            task=task, result=result, other_topics=[], **MODEL)
    assert error.value.code == code and error.value.decision_ids == ["decision_24"]
    assert len(service.model_calls) == 1 and service.model_calls[0]["temperature"] == 0.0
    payload = json.loads(service.prompts[0])
    assert payload["authority"]["fixed_decisions"] == task["fixed_decisions"]
    assert len(payload["authority"]["fixed_decisions"]) == 24
    assert payload["authority"]["fixed_decisions"][-1]["answer"] == task["user_constraints"]["brief_edits"]["fixed_24"]
    assert payload["authority"]["fixed_decisions"][-1]["manually_edited"] is True
    assert payload["authority"]["fixed_decisions"][22]["answer_source"] == "user_excluded"
    assert payload["authority"]["user_constraints"] == task["user_constraints"]
    assert all("question" not in context for context in payload["question_contexts"].values())
    assert set(service.model_calls[0]["json_schema"]["properties"]) == {"checks"}
    for unit_schema in service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"].values():
        assert "confirmed_decision_conflict:decision_24" in unit_schema["enum"]
        assert "confirmed_decision_reopened:decision_24" in unit_schema["enum"]
        assert "exclusion_conflict:decision_23" in unit_schema["enum"]
    assert (result, selected_topic, task) == before


@pytest.mark.asyncio
async def test_multi_batch_manifest_keeps_full_authority_and_rejects_late_chapter_conflict_before_local_scope():
    result, task = manifest(5), authority_task()
    result["chapters"][-1]["goal"] = "本章把已定立即暂停规则替换为累计两次后再暂停"
    # Five legal chapters produce ten independent plan units: the last content
    # belongs to the second batch. A first-batch pass cannot publish the plan.
    BrainstormStageCoordinator._validate_plan(result, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)
    first = {"topic{}_{}".format(index, suffix): "pass" for index in range(4) for suffix in ("goal", "content")}
    second = {"topic4_goal": "pass", "topic4_content": "confirmed_decision_conflict:decision_24"}
    service = StubCreationService([json.dumps({"checks": first}), json.dumps({"checks": second})])
    coordinator = BrainstormStageCoordinator(service)
    before = copy.deepcopy((result, task))
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_plan_consistency(stage="writing", kind="plan", topic={},
            task=task, result=result, other_topics=[], **MODEL)
    assert error.value.code == "confirmed_decision_conflict" and error.value.decision_ids == ["decision_24"]
    assert len(service.model_calls) == 2
    for prompt in service.prompts:
        assert json.loads(prompt)["authority"]["fixed_decisions"] == task["fixed_decisions"]
    assert [unit["id"] for unit in json.loads(service.prompts[-1])["validation_task"]["units"]] == list(second)
    assert (result, task) == before
