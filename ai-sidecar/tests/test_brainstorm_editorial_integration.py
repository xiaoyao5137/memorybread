"""Production writing preparation owns copy and keeps semantic admission."""
import copy

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import prepare, setup


async def capture_compilation(**request):
    coordinator, service = setup([])
    captured = {}
    async def admission(**kwargs):
        captured.update(copy.deepcopy(kwargs))
    coordinator._check_stage_consistency = admission
    result = await prepare(coordinator, stage="writing", **request)
    return coordinator, service, captured, result


@pytest.mark.asyncio
async def test_writing_compiles_and_submits_actual_choices_to_semantic_admission_without_generation():
    skills = [{"key": "complete-policy", "execution_plan": ["保持已有证据安排"] * 40}]
    decisions = [{"question_id": "current-rule", "question": "原表怎样保留？",
        "answer_source": "user", "answer": "原表不动", "user_inputs": ["保留全部原表"],
        "selected_options": [{"id": "a", "label": "原表不动", "tradeoff": "全部原值与列位保持"}]}]
    original = copy.deepcopy((skills, decisions))
    coordinator, service, admitted, result = await capture_compilation(
        selected_skills=skills, decisions=decisions)
    assert service.model_calls == []
    assert admitted["result"] == result and admitted["task"]["program_bound_editorial"] is True
    assert admitted["task"]["user_constraints"]["selected_skills"] == skills
    fixed = admitted["task"]["fixed_decisions"]
    assert fixed[0]["question"] == decisions[0]["question"]
    assert fixed[0]["user_inputs"] == decisions[0]["user_inputs"]
    assert fixed[0]["selected_options"] == decisions[0]["selected_options"]
    assert set(result) == {"question", "next_goals", "memory_brief"}
    assert len(result["question"]["options"]) == 2
    assert [item["option_id"] for item in result["next_goals"]] == ["a", "b"]
    coordinator._verify_editorial_compilation(result=result, task=admitted["task"], topic=admitted["topic"])
    assert (skills, decisions) == original


@pytest.mark.asyncio
async def test_program_compilation_does_not_bypass_a_semantic_policy_rejection():
    coordinator, service = setup([])
    called = []
    async def admission(**kwargs):
        called.append(copy.deepcopy(kwargs))
        raise _StageConsistencyError("confirmed_decision_conflict", ["decision_1"])
    coordinator._check_stage_consistency = admission
    with pytest.raises(_StageConsistencyError) as caught:
        await prepare(coordinator, stage="writing")
    assert caught.value.code == "confirmed_decision_conflict"
    assert len(called) == 1 and service.model_calls == []


@pytest.mark.parametrize("changed", ["description", "missing_branch", "chapter", "late_policy", "authority", "packet"])
@pytest.mark.asyncio
async def test_full_compiled_output_and_current_authority_cannot_be_substituted(changed):
    skills = [{"key": "late-policy", "execution_plan": ["完整保留原始证据"]}]
    coordinator, service, admitted, result = await capture_compilation(selected_skills=skills)
    task = admitted["task"]
    if changed == "description":
        result["question"]["options"][1]["description"] = "改为重新处理数据"
    elif changed == "missing_branch":
        result["next_goals"].pop()
    elif changed == "chapter":
        task["selected_chapter"]["goal"] = "重新生成全部章内容"
    elif changed == "late_policy":
        task["user_constraints"]["selected_skills"][0]["execution_plan"].append("新政策")
    elif changed == "authority":
        task["fixed_decisions"].append({"decision_id": "injected", "answer": "另一个约束"})
    else:
        task["editorial_compilation"]["packet"]["parsed"]["question"]["options"][1]["label"] = "重新计算"
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        coordinator._verify_editorial_compilation(result=result, task=task, topic=admitted["topic"])
    assert service.model_calls == []


@pytest.mark.parametrize("changed", ["result", "policy"])
@pytest.mark.asyncio
async def test_async_admission_cannot_silently_change_a_compiled_candidate(changed):
    coordinator, service = setup([])
    async def admission(**kwargs):
        if changed == "result":
            kwargs["result"]["question"]["options"][1]["description"] = "新编造方法"
        else:
            kwargs["task"]["user_constraints"]["selected_skills"][0]["execution_plan"] = ["被替换规则"]
    coordinator._check_stage_consistency = admission
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await prepare(coordinator, stage="writing", selected_skills=[{"key": "p", "execution_plan": ["原规则"]}])
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_cleared_original_answer_is_not_revived_as_an_already_confirmed_compiler_choice():
    _, _, _, generated = await capture_compilation()
    decisions = [{"question_id": "old", "question": generated["question"]["prompt"],
        "answer_source": "user", "answer": generated["question"]["options"][0]["label"]}]
    _, service, admitted, result = await capture_compilation(decisions=decisions, brief_edits={"old": ""})
    assert admitted["task"]["fixed_decisions"] == []
    assert admitted["task"]["cleared_constraints"][0]["question_id"] == "old"
    assert result["question"]["prompt"] == generated["question"]["prompt"] and service.model_calls == []


@pytest.mark.asyncio
async def test_actual_confirmed_compiled_choice_is_not_reasked():
    _, _, _, generated = await capture_compilation()
    decisions = [{"question_id": "old", "question": generated["question"]["prompt"],
        "answer_source": "user", "answer": generated["question"]["options"][0]["label"],
        "user_inputs": [generated["question"]["options"][0]["label"]]}]
    coordinator, service = setup([])
    with pytest.raises(BrainstormGenerationError, match="已经确认"):
        await prepare(coordinator, stage="writing", decisions=decisions)
    assert service.model_calls == []
