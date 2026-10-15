"""Generated free copy cannot narrate user confirmation state."""
import copy

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator
from creation.brainstorm_confirmation import confirmation_status_violations
from tests.test_brainstorm_stage_nodes import check_result, prepare, review_record, review_units, setup


def candidate(statement="基于已确认的受众定位，安排正文。"):
    return {"question": {"prompt": "周报应主要面向哪些读者？", "options": [
        {"id": "a", "label": "按工作对象介绍", "description": "按读者任务组织说明。"},
        {"id": "b", "label": "按关键结论介绍", "description": "先给结论，再逐项解释。"}]},
        "candidate_copy": {"why_now": statement, "answer_template": "可补充你的需求。"},
        "next_goals": []}


@pytest.mark.parametrize("statement", ["受众尚未确定，先比较读者需求。", "先确认受众，再决定说明方式。",
    "面向管理层组织本期周报。", "沿用用户给定的管理层受众与时间范围。"])
def test_questions_and_direct_facts_do_not_narrate_completed_confirmation(statement):
    assert confirmation_status_violations(candidate(statement)) == []


@pytest.mark.parametrize("statement", ["受众已经确认。", "在已锁定范围内选择方法。", "用户已选定方案。",
    "用户已选择此方法。", "The audience has been confirmed.", "The user already selected this method.",
    "若用户已确认范围，则采用该方法。", "不能声称用户已经确认范围。", "避免输出“用户已选定”这类措辞。"])
def test_completed_status_narration_is_a_language_contract_violation(statement):
    value = candidate(statement)
    before = copy.deepcopy(value)
    assert confirmation_status_violations(value) == [
        {"unit_id": "q0_why_now", "field": "questions[0].why_now"}]
    assert value == before


def test_authority_and_source_excerpts_are_never_examined_or_modified():
    value = candidate("面向管理层组织本期周报。")
    value["authority"] = {"root_request": "受众已确认是管理层。", "brief_edits": {"goal": "已锁定范围"}}
    value["question"]["context_details"] = "源文献：用户已确认范围。"
    for option in value["question"]["options"]:
        option["details"] = "源引文：用户已经选定方法。"
    before = copy.deepcopy(value)
    assert confirmation_status_violations(value) == [] and value == before


def test_every_generated_field_and_future_goal_is_checked_separately():
    value = candidate()
    value["question"]["prompt"] = "在范围已锁定的前提下，采用何种方式？"
    value["candidate_copy"]["single_choice_reason"] = "用户已经选定一种优先级。"
    value["candidate_copy"]["answer_template"] = "可补充已确定的方法。"
    value["question"]["options"][1]["description"] = "此前确认过此方案，直接沿用。"
    value["next_goals"] = [{"option_id": "a", "goals": ["沿用已确认的时间范围"]}]
    value["root_goals"] = ["沿用已经锁定的对象"]
    violations = confirmation_status_violations(value)
    assert [unit["unit_id"] for unit in violations] == ["q0_prompt", "q0_why_now", "q0_single_choice_reason",
        "q0_answer_template", "q0_o1", "g0_0", "root0"]


@pytest.mark.parametrize("field,text", [
    ("why_now", "基于已确认的受众定位，需确定报告核心叙事逻辑以支撑决策"),
    ("prompt", "在已锁定管理层与跨部门协作方为受众的前提下，本周应优先呈现什么？"),
])
@pytest.mark.asyncio
async def test_original_failure_copy_as_a_new_free_candidate_rejects_without_model_calls(field, text):
    record = review_record()
    record["question"][field] = text
    before = copy.deepcopy(record)
    coordinator, service = setup([], check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="candidate_as_confirmed") as error:
        await coordinator._check_stage_consistency(stage="direction", kind="node", topic=record["topic"],
            task={"fixed_decisions": [], "ancestor_conditions": [], "goal": record["topic"]["goal"],
                "cleared_constraints": [], "user_constraints": {"root_request": "请生成GPU成本优化周报"}},
            result={"question": record["question"], "next_goals": []}, other_topics=[])
    assert error.value.failed_checks == [{"unit_id": "q0_" + field,
        "field": "questions[0]." + field}]
    assert service.model_calls == [] and record == before


@pytest.mark.asyncio
async def test_existing_review_uses_original_fact_gates_without_new_syntax_ban():
    record = review_record()
    record["question"]["why_now"] = "受众已确认为管理层，比较具体说明方式。"
    before = copy.deepcopy(record)
    coordinator, service = setup([check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, root_request="给管理层写周报。")
    assert result["review"] == "pass" and len(service.model_calls) == 1
    assert record == before


@pytest.mark.parametrize("kind,result", [
    ("plan", {"topics": [{"id": "topic", "title": "方向", "goal": "沿用已确认的对象范围"}],
        "chapters": [], "open_flags": []}),
    ("topic_plan", {"root_goals": ["沿用已经锁定的对象范围"]}),
])
@pytest.mark.asyncio
async def test_free_plans_and_topic_roots_are_guarded_before_model_checking(kind, result):
    coordinator, service = setup([], check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="candidate_as_confirmed"):
        await coordinator._check_stage_consistency(stage="direction", kind=kind,
            topic={"id": "topic", "title": "方向", "goal": "比较范围"},
            task={"fixed_decisions": [], "ancestor_conditions": [], "goal": "比较范围",
                "cleared_constraints": [], "user_constraints": {"root_request": "生成周报"}},
            result=result, other_topics=[])
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_verified_program_editorial_compilation_does_not_use_free_candidate_language_ban(monkeypatch):
    response = check_result(unit_ids=review_units(),
        option_labels={"a": "先概括再解释", "b": "先解释再归纳"})
    coordinator, service = setup([response], check_consistency=True)
    monkeypatch.setattr("creation.brainstorm_stages.confirmation_status_violations",
        lambda candidate: pytest.fail("Trusted editorial compilation must keep its original proof protocol"))
    result = await prepare(coordinator, stage="writing")
    assert [option["label"] for option in result["question"]["options"]] == ["先概括再解释", "先解释再归纳"]
    assert len(service.model_calls) == 1


@pytest.mark.parametrize("body,code,matching_prefix", [
    ("说明段落按什么方式组织？", None, True),
    ("在已确认的表达方式内，采用什么顺序？", "candidate_as_confirmed", True),
    ("说明段落按什么方式组织？", "candidate_as_confirmed", False),
])
@pytest.mark.asyncio
async def test_trusted_ancestor_status_words_are_preserved_while_only_new_body_uses_syntax_contract(body, code, matching_prefix):
    record = review_record()
    condition = {"parent_question_id": "old-parent", "parent_option_id": "a",
        "parent_prompt": "原表达方式是什么？", "option_label": "用户已确认的表达方式",
        "option_description": "受保护的原完整选择文字，不由本次模型生成。"}
    task = {"fixed_decisions": [], "cleared_constraints": [], "goal": "选择说明组织方式",
        "ancestor_conditions": [condition], "user_constraints": {"root_request": "保留原表达方式。"}}
    prefix = BrainstormStageCoordinator._node_condition_prefix(task)
    record["question"].update(prompt=(prefix if matching_prefix else "若「用户已确认的假前缀」，") + body,
        parent_question_id="old-parent", parent_option_id="a")
    result = {"question": record["question"], "next_goals": []}
    before = copy.deepcopy((task, result))
    coordinator, service = setup([check_result(unit_ids=review_units())], check_consistency=True)
    kwargs = {"stage": "detail", "kind": "node", "topic": record["topic"],
        "task": task, "result": result, "other_topics": [],
        "creation_model": None, "creation_api_key": None, "creation_base_url": None}
    if code:
        with pytest.raises(BrainstormGenerationError, match=code):
            await coordinator._check_stage_consistency(**kwargs)
        assert service.model_calls == []
    else:
        await coordinator._check_stage_consistency(**kwargs)
        assert len(service.model_calls) == 1
        import json
        context = json.loads(service.prompts[0])["question_contexts"]["q0"]
        assert context["question"]["prompt"] == prefix + body
        assert context["ancestor_conditions"] == [condition]
    assert (task, result) == before
