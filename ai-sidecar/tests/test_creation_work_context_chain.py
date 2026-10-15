"""Work preferences cross writing and delivery without becoming sample facts."""
import copy
import json

import pytest

from creation.agent_loop import CreationAgentLoop
from creation.delivery_contract import delivery_source_materials, supplied_material_lines
from creation.operations import OperationError
from creation.service import CreationOptions, CreationService
from creation.work_quality import normalize_work_context
from creation.work_quality import work_acceptance_checks
from tests.test_creation_delivery_contract import DeliveryService
from tests.test_creation_operations import run_args


CONTEXT = {"purpose": "评审试点", "decision_goal": "决定是否启动", "constraints": "预算上限3000元",
           "style_samples": ["旧项目已收入999万元。"], "profile_id": "proposal_review", "review_enabled": True}


def state_for(context=None):
    args = run_args("", "写试点方案")
    args["options"] = CreationOptions(work_context=context)
    loop = CreationAgentLoop(DeliveryService())
    return loop, loop._new_state(**args, model_mode="local")


def test_default_control_does_not_add_context_or_change_report_style():
    loop, state = state_for()
    assert CreationOptions().work_context is None
    assert loop._input_context(state)["user_options"] == {}
    assert CreationService._infer_style(None, "写一份项目汇报") == "专业清晰"
    assert CreationService._infer_style(None, "写一份正式汇报") == "正式严谨"


@pytest.mark.parametrize("invalid", [[], False, 0, ""])
def test_invalid_work_context_is_not_silently_treated_as_legacy_default(invalid):
    with pytest.raises(OperationError):
        CreationOptions(work_context=invalid)


def test_fact_settings_and_expression_examples_have_separate_paths():
    loop, state = state_for(CONTEXT)
    context = loop._input_context(state)
    assert "style_samples" not in context["user_options"]["work_context"]
    assert state.environment["work_context"]["style_samples"] == CONTEXT["style_samples"]
    supplied = supplied_material_lines(state.user_message, "", [], user_options={"work_context": CONTEXT})
    assert supplied["user-options-work-constraints-1"] == "预算上限3000元"
    assert "999" not in json.dumps(supplied, ensure_ascii=False)
    material = delivery_source_materials(state.user_message, {"input_context": {"user_options": {"work_context": CONTEXT}}})
    assert "999" not in json.dumps(material, ensure_ascii=False)


def test_work_settings_survive_checkpoint_and_changes_invalidate_resume():
    loop, state = state_for(CONTEXT)
    loop._input_context(state)
    loop._validate_resume_context(state, state.session_id, "", work_context=CONTEXT)
    with pytest.raises(OperationError) as error:
        loop._validate_resume_context(state, state.session_id, "", work_context={**CONTEXT, "decision_goal": "决定是否暂停"})
    assert error.value.code == "CREATION_BASE_CHANGED"
    assert state.environment["work_context"] == normalize_work_context(CONTEXT)


def test_direct_work_brief_remains_direct_and_carries_no_brainstorm_decisions():
    args = run_args("", "写试点方案")
    args.update(creation_brief={"work_context": CONTEXT}, creation_mode="direct")
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    assert state.creation_mode == "direct"
    assert state.options["work_context"] == normalize_work_context(CONTEXT)
    assert not loop._input_context(state).get("brainstorm_decisions")


@pytest.mark.asyncio
async def test_delivery_semantic_boundary_uses_adopted_receipt_not_candidate_catalog():
    loop, state = state_for(CONTEXT)
    candidate = {"id": "catalog-only", "title": "未采用的候选", "skillInstructions": "必须扩写全部栏目"}
    adopted = {"id": "adopted", "title": "事实表格", "strictStructure": True,
        "skillInstructions": "只输出事实表格，不新增判断栏目", "executionSteps": [
            {"id": "facts", "title": "事实表格", "objective": "按已有事实形成表格", "output_role": "document"}]}
    support = {"id": "support", "title": "语言规则", "workflowRole": "support",
               "executionSteps": [{"id": "unrelated", "title": "不应成为全篇流程"}]}
    state.selected_skills = [candidate, adopted, support]
    state.environment.update(input_contract={"deliverable": "事实表格", "acceptance": []},
                             applied_skills=[adopted, support])
    captured = []

    async def review(instruction, document, contract, environment):
        captured.append(copy.deepcopy(environment["work_quality_skill_constraints"]))
        return {"status": "pass"}

    loop.service.review_creation_delivery = review
    await loop._review_creation_delivery(state, "已有正文")
    assert [item["id"] for item in captured[-1]] == ["adopted", "support"]
    assert captured[-1][0]["skill_instructions"] == adopted["skillInstructions"]
    assert captured[-1][1]["execution_steps"] == []
    state.environment["applied_skills"] = []
    await loop._review_creation_delivery(state, "已有正文")
    assert captured[-1] == []


@pytest.mark.asyncio
async def test_adopted_semantic_rules_preserve_long_tail_and_all_declared_steps():
    loop, state = state_for(CONTEXT)
    instructions = "已确认的写作规则。" * 100 + "末尾白名单：只能保留两列，禁止新增建议。"
    objective = "执行说明。" * 150 + "最后只填入已确认事实。"
    skill = {"id": "long-rules", "title": "表格", "skillInstructions": instructions,
             "executionSteps": [{"id": str(i), "objective": objective, "output_role": "section"}
                                for i in range(30)]}
    state.environment.update(input_contract={"deliverable": "表格", "acceptance": []}, applied_skills=[skill])
    captured = []

    async def review(instruction, document, contract, environment):
        captured.append(copy.deepcopy(environment["work_quality_skill_constraints"]))
        return {"status": "pass"}

    loop.service.review_creation_delivery = review
    await loop._review_creation_delivery(state, "已有正文")
    assert captured[-1][0]["skill_instructions"] == instructions
    assert len(captured[-1][0]["execution_steps"]) == 30
    assert captured[-1][0]["execution_steps"][-1]["objective"] == objective
    # The existing repair prompt keeps its independent compaction behavior.
    assert len(loop._repair_skill_constraints(state)[0]["skill_instructions"]) < len(instructions)


@pytest.mark.asyncio
async def test_rebuilt_brainstorm_acceptance_restores_explicit_work_checks():
    from tests.test_creation_delivery_contract import contract
    args = run_args("", "写试点方案")
    args.update(creation_mode="brainstorm", creation_brief={"decisions": [
        {"question_id": "scope", "dimension": "范围", "summary": "只验证素材交接", "source": "user"}]},
        options=CreationOptions(work_context=CONTEXT))
    loop = CreationAgentLoop(DeliveryService())
    state = loop._new_state(**args, model_mode="local")
    loop._input_context(state)
    state.environment.update(input_contract=contract(), brainstorm_acceptance_version=9,
        operation={"kind": "generate"}, input_base_document="", delivery_checked_hash="obsolete")
    await loop._refresh_brainstorm_acceptance(state)
    identifiers = {item["id"] for item in state.environment["input_contract"]["acceptance"]}
    assert {item["id"] for item in work_acceptance_checks(CONTEXT)}.issubset(identifiers)
    assert "delivery_checked_hash" not in state.environment
