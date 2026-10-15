"""A checked body container preserves source policy, without choosing methods."""
import asyncio
import copy
import json

import httpx
import pytest

from creation.brainstorm import (
    BrainstormGenerationError, BrainstormGenerationTimeout, BrainstormOutputTruncated,
)
from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_plan_authority import manifest_checks
from tests.test_brainstorm_stage_nodes import fixed_decision, prepare, setup
from tests.test_brainstorm_stages import plan


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}


def atomic(edits=None):
    return BrainstormStageCoordinator._atomic_writing_plan(edits or {})


@pytest.mark.parametrize("axis", BrainstormStageCoordinator.WRITING_AXES)
@pytest.mark.parametrize("prefix", ["", "本章完整执行："])
def test_body_assignment_cannot_reuse_program_owned_organization_namespace(axis, prefix):
    value = plan("writing")
    value["chapters"][0]["goal"] = prefix + axis["goal"].split("，", 1)[-1]
    before = copy.deepcopy(value)
    with pytest.raises(BrainstormGenerationError, match="正文任务.*组织能力"):
        BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert value == before
    assert BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS,
        require_writing_axis=False)["chapters"][0]["goal"] == value["chapters"][0]["goal"]


def test_body_and_unresolved_organization_are_different_types():
    value = atomic()
    before = copy.deepcopy(value)
    result = BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert result == before and value == before
    assert result["chapters"] == [{"id": "body", "title": "创作正文",
        "goal": "按当前需求、完整有效确认简报和所选技能规定的结构，创作本次任务的完整正文；保留原有分节、必需内容、固定方法与证据位置。"}]
    assert result["topics"][0]["goal"] == BrainstormStageCoordinator.WRITING_AXES[0]["goal"]
    assert result["topics"][0]["chapter_id"] == result["chapters"][0]["id"]


@pytest.mark.parametrize("flags,expected", [
    ("", []), ("  ", []), ("来源尚待用户决定；范围待确认", ["来源尚待用户决定；范围待确认"]),
    ("第一条完整事项\n\n第二条完整事项", ["第一条完整事项", "第二条完整事项"]),
])
def test_atomic_flags_only_use_complete_current_edited_lines(flags, expected):
    edits = {"open_flags": flags, "body": "原有各节不得改变"}
    before = copy.deepcopy(edits)
    assert atomic(edits)["open_flags"] == expected and edits == before


@pytest.mark.parametrize("flags", [None, [], {}, 42])
def test_invalid_current_flag_input_is_not_repaired_into_an_atomic_plan(flags):
    with pytest.raises(BrainstormGenerationError, match="修订格式"):
        atomic({"open_flags": flags})


@pytest.mark.parametrize("flags", ["x" * 241, "\n".join(str(index) for index in range(13))])
def test_atomic_flag_budget_never_truncates_current_user_items(flags):
    with pytest.raises(BrainstormGenerationError):
        BrainstormStageCoordinator._validate_plan(atomic({"open_flags": flags}), "writing",
            BrainstormStageCoordinator.DEFAULT_LIMITS)


@pytest.mark.asyncio
async def test_default_writing_plan_has_no_free_generation_and_preserves_complete_authority():
    edits = {"open_flags": "真实尚待决定的当前事项"}
    expected = atomic(edits)
    skills = [{"id": "report", "executionSteps": [{"instruction": "保留五个原有板块和完整证据位置。",
        "metadata": {"full_policy": "完整政策" * 300}}]}]
    decisions = [fixed_decision() for _ in range(24)]
    for index, decision in enumerate(decisions):
        decision["question_id"] = "fixed{}".format(index)
    decisions.append({"question_id": "cleared", "question": "旧事项", "answer": "",
        "answer_source": "user_cleared", "user_inputs": []})
    coordinator, service = setup([manifest_checks(expected)], check_consistency=True)
    def forbidden(*args, **kwargs):
        pytest.fail("Atomic planning must not use a compact model-generation context")
    coordinator._node_model_context = forbidden
    coordinator._stage_schema = forbidden
    before = copy.deepcopy((edits, skills, decisions))
    result = await prepare(coordinator, stage="writing", kind="plan", root_request="完整实际创作需求",
        decisions=decisions, brief_edits=edits, selected_skills=skills)
    assert result == expected and len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    assert len(payload["authority"]["fixed_decisions"]) == 24
    assert len(payload["authority"]["cleared_constraints"]) == 1
    assert payload["authority"]["user_constraints"]["selected_skills"] == skills
    assert payload["authority"]["user_constraints"]["root_request"] == "完整实际创作需求"
    assert payload["question_contexts"]["topic0"]["chapter_content"] == expected["chapters"][0]
    assert payload["validation_task"]["units"][-1]["text"] == edits["open_flags"]
    assert all(call["num_predict"] == 1200 and call["temperature"] == 0.0 for call in service.model_calls)
    assert (edits, skills, decisions) == before


@pytest.mark.asyncio
async def test_recompiled_atomic_plan_keeps_user_confirmation_words_outside_new_free_copy_ban():
    edits = {"root_request": "受众已确认为管理层，按原需求创作。",
        "open_flags": "用户已确认受众；本次仍需决定说明粒度。"}
    expected = atomic(edits)
    coordinator, service = setup([manifest_checks(expected)], check_consistency=True)
    before = copy.deepcopy(edits)
    result = await prepare(coordinator, stage="writing", kind="plan", root_request="旧需求",
        brief_edits=edits)
    assert result == expected and edits == before and len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    assert payload["authority"]["user_constraints"]["root_request"] == edits["root_request"]
    assert payload["authority"]["user_constraints"]["brief_edits"] == edits
    assert payload["validation_task"]["units"][-1]["text"] == edits["open_flags"]


@pytest.mark.parametrize("mutation", [
    lambda result, task: task.update(program_bound_writing_plan=True),
    lambda result, task: result["topics"][0].update(goal="沿用已锁定的其它方案"),
    lambda result, task: result["chapters"][0].update(goal="把其它内容写入正文"),
    lambda result, task: result["open_flags"].append("另一个已确认范围"),
    lambda result, task: task["program_bound_writing_plan"]["limits"].pop("max_depth"),
])
@pytest.mark.asyncio
async def test_atomic_plan_marker_without_exact_recompilation_never_bypasses_syntax_gate(mutation):
    result = atomic()
    task = {"fixed_decisions": [], "cleared_constraints": [], "ancestor_conditions": [], "goal": "正文组织",
        "user_constraints": {"root_request": "完整当前需求", "brief_edits": {}, "selected_skills": []},
        "program_bound_writing_plan": {"limits": dict(BrainstormStageCoordinator.DEFAULT_LIMITS)}}
    mutation(result, task)
    coordinator, service = setup([], check_consistency=True)
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await coordinator._check_stage_consistency(stage="writing", kind="plan", topic={}, task=task,
            result=result, other_topics=[], **MODEL)
    assert service.model_calls == []


@pytest.mark.parametrize("code", ["topic_scope", "confirmed_decision_reopened", "confirmed_decision_conflict",
    "parent_condition_conflict", "exclusion_conflict", "cleared_constraint_conflict", "candidate_as_confirmed",
    "checker_invalid", "insufficient_context", "checker_unavailable", "source_permission_revoked"])
@pytest.mark.asyncio
async def test_every_real_gate_rejection_keeps_its_identity_without_regeneration(code):
    coordinator, service = setup([], check_consistency=True)
    calls = []
    async def reject(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raise _StageConsistencyError(code, ["decision_1"])
    coordinator._check_plan_consistency = reject
    with pytest.raises(_StageConsistencyError) as error:
        await prepare(coordinator, stage="writing", kind="plan", decisions=[fixed_decision()])
    assert error.value.code == code and error.value.decision_ids == ["decision_1"]
    assert len(calls) == 1 and calls[0]["result"] == atomic() and not service.model_calls


@pytest.mark.parametrize("response", [{"checks": {}}, {"checks": {"fake": "pass"}},
    {"checks": {"topic0_goal": "pass", "topic0_content": "insufficient_context"}}])
@pytest.mark.asyncio
async def test_incomplete_or_unknown_actual_checker_response_never_publishes_a_program_plan(response):
    coordinator, service = setup([response], check_consistency=True)
    with pytest.raises(_StageConsistencyError):
        await prepare(coordinator, stage="writing", kind="plan")
    assert len(service.model_calls) == 1
    assert "checks" in service.model_calls[0]["json_schema"]["properties"]


@pytest.mark.parametrize("error", [BrainstormOutputTruncated("truncated"),
    BrainstormGenerationTimeout("provider timed out"), BrainstormGenerationError("empty provider response"),
    asyncio.TimeoutError("provider timeout"), RuntimeError("unknown provider failure")])
@pytest.mark.asyncio
async def test_provider_failure_of_the_real_gate_is_not_packaged_as_success(error):
    coordinator, _service = setup([], check_consistency=True)
    calls = []
    async def fail(*args, **kwargs):
        calls.append(kwargs)
        raise error
    coordinator._complete = fail
    # The existing semantic gate deliberately normalizes provider details into
    # checker_unavailable, rather than exposing them as a candidate result.
    with pytest.raises(_StageConsistencyError) as caught:
        await prepare(coordinator, stage="writing", kind="plan")
    assert caught.value.code == "checker_unavailable"
    assert len(calls) == 1 and "checks" in calls[0]["json_schema"]["properties"]


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 499, 503])
@pytest.mark.asyncio
async def test_auth_input_and_transient_errors_remain_original_real_gate_errors(status):
    error = httpx.HTTPStatusError("provider", request=httpx.Request("POST", "https://example.test"),
        response=httpx.Response(status))
    coordinator, _service = setup([], check_consistency=True)
    calls = []
    async def fail(*args, **kwargs):
        calls.append(kwargs)
        raise error
    coordinator._complete = fail
    with pytest.raises(_StageConsistencyError) as caught:
        await prepare(coordinator, stage="writing", kind="plan")
    assert caught.value.code == "checker_unavailable" and len(calls) == 1


@pytest.mark.asyncio
async def test_current_root_manual_exclusion_and_clear_sources_are_unchanged():
    current = fixed_decision(question="保留什么？", answer="原范围")
    current["question_id"] = "current"
    excluded = {"question_id": "exclude", "question": "是否读取旧资料？", "answer": "",
        "user_inputs": [], "answer_source": "user_excluded", "excluded_topic": "旧资料"}
    cleared = {"question_id": "clear", "question": "原范围", "answer": "", "user_inputs": [],
        "answer_source": "user_cleared"}
    edits = {"root_request": "当前人工修订全文需求", "current": "不要纳入已取消对象", "open_flags": ""}
    coordinator, service = setup([manifest_checks(atomic(edits))], check_consistency=True)
    result = await prepare(coordinator, stage="writing", kind="plan", root_request="旧需求",
        decisions=[current, excluded, cleared], brief_edits=edits)
    assert result == atomic(edits)
    authority = json.loads(service.prompts[0])["authority"]
    assert authority["user_constraints"]["root_request"] == edits["root_request"]
    assert authority["fixed_decisions"][0]["answer"] == edits["current"]
    assert authority["fixed_decisions"][0]["manually_edited"] is True
    assert authority["fixed_decisions"][1]["answer_source"] == "user_excluded"
    assert authority["fixed_decisions"][1]["excluded_topic"] == "旧资料"
    assert authority["cleared_constraints"][0]["question_id"] == "clear"


@pytest.mark.parametrize("maximum,depth,questions", [(1, 2, 3), (2, 2, 12), (6, 2, 24), (1, 3, 7), (12, 3, 24)])
@pytest.mark.asyncio
async def test_atomic_unit_respects_valid_configured_budget_without_truncating_internal_sections(maximum, depth, questions):
    coordinator, service = setup([manifest_checks(atomic())], check_consistency=True)
    result = await prepare(coordinator, stage="writing", kind="plan", limits={"max_topics": maximum,
        "max_depth": depth, "max_questions_per_topic": questions})
    assert result == atomic() and len(service.model_calls) == 1


@pytest.mark.parametrize("limits", [{"max_topics": 0}, {"max_depth": 1}, {"max_questions_per_topic": 2},
    {"max_topics": 13}, {"max_questions_per_topic": True}])
@pytest.mark.asyncio
async def test_invalid_input_budget_is_rejected_before_the_program_factory_or_any_model(limits):
    coordinator, service = setup([], check_consistency=True)
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, stage="writing", kind="plan", limits=limits)
    assert not service.model_calls


@pytest.mark.asyncio
async def test_real_gate_timeout_uses_existing_total_deadline_and_closes_its_stream(monkeypatch):
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.03)
    coordinator, _service = setup([], check_consistency=True)
    closed = asyncio.Event()
    calls = []
    async def block(*args, **kwargs):
        calls.append(kwargs)
        try:
            await asyncio.sleep(10)
            return "{}"
        finally:
            closed.set()
    coordinator._complete = block
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, stage="writing", kind="plan")
    assert closed.is_set() and len(calls) == 1


@pytest.mark.asyncio
async def test_external_cancel_closes_the_real_gate_without_success_or_a_retry():
    coordinator, _service = setup([], check_consistency=True)
    started, closed = asyncio.Event(), asyncio.Event()
    calls = []
    async def block(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        try:
            await asyncio.sleep(10)
            return "{}"
        finally:
            closed.set()
    coordinator._complete = block
    running = asyncio.create_task(prepare(coordinator, stage="writing", kind="plan"))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert closed.is_set() and len(calls) == 1
