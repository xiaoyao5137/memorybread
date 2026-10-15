"""Writing plans own local chapter work while inheriting document constraints."""

import copy
import json
import re

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_plan_authority import manifest_checks
from tests.test_brainstorm_stage_nodes import fixed_decision, prepare, setup

MODEL = {"creation_model": "test-model", "creation_api_key": "", "creation_base_url": ""}


def writing_plan(count=3):
    goals = ["界定本期数据的对象与口径", "比较本期各项收支结果", "说明风险证据与后续行动"]
    chapters = [{"id": "chapter{}".format(index), "title": "独立章节{}".format(index),
        "goal": goals[index] if index < len(goals) else "解释独立事项{}的证据与步骤".format(index)}
        for index in range(count)]
    return {"topics": [{"id": "topic{}".format(index), "title": chapter["title"],
        "goal": BrainstormStageCoordinator.WRITING_AXES[0]["goal"], "chapter_id": chapter["id"]}
        for index, chapter in enumerate(chapters)], "chapters": chapters, "open_flags": []}


def scope_checks(value, current=None):
    refs = ["chapter{}".format(index) for index in range(len(value["chapters"]))]
    current = refs if current is None else current
    evidence = BrainstormStageCoordinator._chapter_scope_evidence(value["chapters"])
    return {"chapter_checks": {ref: {
        "chapter_evidence_ref": evidence[ref]["evidence_ref"],
        "title_evidence": value["chapters"][refs.index(ref)]["title"],
        "goal_evidence": value["chapters"][refs.index(ref)]["goal"],
        "facts": ["no", "no"],
        "siblings": {other: {"sibling_evidence_ref": evidence[other]["evidence_ref"], "facts": ["no", "no"]}
            for other in refs if other != ref},
    } for ref in current}}


def task(decisions=None):
    return {"fixed_decisions": decisions or [], "cleared_constraints": [],
        "user_constraints": {"root_request": "编写各章分工互补的分析报告", "brief_edits": {}}}


def test_default_model_cannot_publish_same_whole_document_goal_under_different_chapter_titles():
    value = writing_plan()
    value["chapters"][1]["goal"] = " 界定 本期数据的对象与口径。 "
    before = copy.deepcopy(value)
    with pytest.raises(BrainstormGenerationError, match="正文任务重复"):
        BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert value == before
    # Catalog ownership is governed by its existing trusted contract.
    assert BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS,
        require_writing_axis=False)["chapters"][1]["goal"] == value["chapters"][1]["goal"].strip()


@pytest.mark.asyncio
async def test_independent_scope_rejects_normalized_full_document_goals_without_a_model_call():
    value = writing_plan()
    goal = "陈述已确认事实；展示口径、收支、风险与行动。"
    value["chapters"][0]["goal"] = goal
    value["chapters"][1]["goal"] = " 陈述已确认事实：展示口径，收支，风险与行动 "
    before = copy.deepcopy(value)
    coordinator, service = setup([])
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert error.value.code == "topic_scope"
    assert service.prompts == []
    assert service.model_calls == []
    assert value == before


@pytest.mark.asyncio
async def test_distinct_local_chapter_tasks_can_share_all_confirmed_background_and_format():
    value = writing_plan()
    for chapter in value["chapters"]:
        chapter["goal"] += "；共同继承已定周期、来源与格式"
    decisions = [{"decision_id": "fixed{}".format(index), "answer_source": "user",
        "question": "已确认背景{}".format(index), "answer": "共同固定要求{}".format(index)} for index in range(24)]
    coordinator, service = setup([scope_checks(value)])
    before = copy.deepcopy((value, decisions))
    await coordinator._check_writing_plan_scope(result=value, task=task(decisions), **MODEL)
    payload = json.loads(service.prompts[0])
    assert len(payload["authority"]["fixed_decisions"]) == 24
    assert {key: payload["chapter_evidence"]["chapter1"][key] for key in ("id", "title", "goal")} == value["chapters"][1]
    assert payload["validation_task"]["chapter_refs"] == ["chapter0", "chapter1", "chapter2"]
    assert (value, decisions) == before
    assert service.model_calls[0]["num_predict"] == 4000


@pytest.mark.parametrize("location", ["whole_document", "only_constraints", "same_sibling", "takes_over_sibling"])
@pytest.mark.asyncio
async def test_scope_rejects_whole_document_work_and_duplicate_sibling_tasks_even_with_inheritance_claim(location):
    value = writing_plan()
    value["chapters"][0]["goal"] = "继承全部共同约束；本章完整撰写对象口径、所有收支结果、风险证据与后续行动"
    checked = scope_checks(value)
    if location in ("whole_document", "only_constraints"):
        checked["chapter_checks"]["chapter0"]["facts"][0 if location == "whole_document" else 1] = "yes"
    else:
        checked["chapter_checks"]["chapter0"]["siblings"]["chapter1"]["facts"][0 if location == "same_sibling" else 1] = "yes"
    coordinator, _service = setup([checked])
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert error.value.code == "topic_scope"


@pytest.mark.parametrize("mutation", [
    lambda checked: checked["chapter_checks"]["chapter0"].update(title_evidence="别章标题"),
    lambda checked: checked["chapter_checks"]["chapter0"].update(goal_evidence="任意旧任务"),
    lambda checked: checked["chapter_checks"]["chapter0"].update(chapter_evidence_ref="chapter1"),
    lambda checked: checked["chapter_checks"]["chapter0"]["siblings"]["chapter1"].update(sibling_evidence_ref="chapter2"),
    lambda checked: checked["chapter_checks"]["chapter0"]["siblings"].pop("chapter1"),
    lambda checked: checked["chapter_checks"]["chapter0"]["siblings"].update(extra={"facts": ["no", "no"]}),
    lambda checked: checked["chapter_checks"]["chapter0"].update(facts=["no"]),
    lambda checked: checked["chapter_checks"]["chapter0"].update(facts=[False, "no"]),
    lambda checked: checked.update(chapter_checks="pass"),
])
@pytest.mark.asyncio
async def test_scope_evidence_and_complete_sibling_coverage_are_mandatory(mutation):
    value = writing_plan()
    checked = scope_checks(value)
    mutation(checked)
    coordinator, _service = setup([checked])
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert error.value.code == "checker_invalid"


@pytest.mark.parametrize("field", ["title", "goal"])
@pytest.mark.asyncio
async def test_sibling_evidence_cannot_replay_after_its_content_changes_under_the_same_id(field):
    value = writing_plan()
    original = scope_checks(value)
    value["chapters"][1][field] += "；补充本章新的具体职责"
    checked = scope_checks(value)
    checked["chapter_checks"]["chapter0"]["siblings"]["chapter1"]["sibling_evidence_ref"] = (
        original["chapter_checks"]["chapter0"]["siblings"]["chapter1"]["sibling_evidence_ref"])
    assert checked["chapter_checks"]["chapter0"]["chapter_evidence_ref"] == (
        original["chapter_checks"]["chapter0"]["chapter_evidence_ref"])
    coordinator, _service = setup([checked])
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert error.value.code == "checker_invalid"


@pytest.mark.parametrize("sibling", [False, True])
@pytest.mark.asyncio
async def test_scope_unknown_closes_the_gate_for_current_chapter_and_sibling(sibling):
    value = writing_plan()
    checked = scope_checks(value)
    target = checked["chapter_checks"]["chapter0"]
    if sibling:
        target = target["siblings"]["chapter1"]
    target["facts"][0] = "unknown"
    coordinator, _service = setup([checked])
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert error.value.code == "insufficient_context"


@pytest.mark.asyncio
async def test_legacy_multi_chapter_scope_still_rejects_duplicate_document_assignments():
    bad = writing_plan()
    bad["chapters"][0]["goal"] = "本章完整撰写口径、收支、风险与行动"
    rejected = scope_checks(bad)
    rejected["chapter_checks"]["chapter0"]["facts"][0] = "yes"
    decision = fixed_decision(question="保留哪些共同要求？", answer="固定周期与来源必须保留")
    coordinator, service = setup([manifest_checks(bad), rejected], check_consistency=True)
    original = copy.deepcopy((bad, decision))
    fixed, cleared = coordinator._stage_authority([decision], {})
    checked_task = {**task(fixed), "cleared_constraints": cleared, "ancestor_conditions": []}
    with pytest.raises(_StageConsistencyError) as error:
        await coordinator._check_plan_consistency(stage="writing", kind="plan", topic={},
            result=bad, task=checked_task, other_topics=[], **MODEL)
    assert error.value.code == "topic_scope"
    bad_scope = json.loads(service.prompts[1])
    assert bad_scope["chapter_evidence"]["chapter0"]["goal"] == bad["chapters"][0]["goal"]
    assert bad_scope["authority"]["fixed_decisions"] == json.loads(service.prompts[0])["authority"]["fixed_decisions"]
    assert (bad, decision) == original
    assert [call["num_predict"] for call in service.model_calls] == [1200, 4000]


@pytest.mark.asyncio
async def test_single_chapter_article_may_own_the_entire_document_without_extra_scope_calls():
    value = writing_plan(1)
    value["chapters"][0]["goal"] = "完整撰写本篇文章的对象口径、收支、风险与行动"
    coordinator, service = setup([manifest_checks(value)], check_consistency=True)
    await coordinator._check_plan_consistency(stage="writing", kind="plan", topic={},
        result=value, task={**task(), "ancestor_conditions": []}, other_topics=[], **MODEL)
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_scope_four_chapter_batches_still_compare_every_sibling_without_truncation():
    value = writing_plan(12)
    responses = [scope_checks(value, ["chapter{}".format(index) for index in range(offset, offset + 4)])
        for offset in range(0, 12, 4)]
    coordinator, service = setup(responses)
    await coordinator._check_writing_plan_scope(result=value, task=task(), **MODEL)
    assert len(service.model_calls) == 3
    for prompt, call in zip(service.prompts, service.model_calls):
        payload = json.loads(prompt)
        assert len(payload["chapter_evidence"]) == 12
        assert len(payload["validation_task"]["chapter_refs"]) == 4
        schemas = call["json_schema"]["properties"]["chapter_checks"]["properties"]
        assert all(len(schema["properties"]["siblings"]["properties"]) == 11 for schema in schemas.values())
        assert call["num_predict"] == 4000


@pytest.mark.parametrize("bad_id", ["章节一", "chapter 1", "chapter/1", "chapter.1", "a" * 81])
def test_plan_decoder_ids_match_runtime_ascii_identifier_contract(bad_id):
    schema = BrainstormStageCoordinator._stage_schema("plan", "writing", BrainstormStageCoordinator.DEFAULT_LIMITS,
        {"sources": []})
    chapter_properties = schema["properties"]["chapters"]["items"]["properties"]
    topic_properties = schema["properties"]["topics"]["items"]["properties"]
    for field in (chapter_properties["id"], topic_properties["id"], topic_properties["chapter_id"]):
        assert re.fullmatch(field["pattern"], bad_id) is None
        assert re.fullmatch(field["pattern"], "chapter_01-a")
    value = writing_plan(1)
    value["chapters"][0]["id"] = bad_id
    value["topics"][0]["chapter_id"] = bad_id
    with pytest.raises(BrainstormGenerationError, match="稳定标识|内容预算"):
        BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)
