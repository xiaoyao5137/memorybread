"""Unsupported negative relations require complete, scoped rule evidence."""
import asyncio
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import (
    check_result, fixed_decision, prepare, review_record, review_units, setup,
)
from tests.test_creation_brainstorm import StubCreationService


def scenario():
    selected = {"id": "topic", "title": "背景说明段", "chapter_id": "chapter1",
        "goal": "量化结果仍优先，只比较之后背景说明段的时间与来源讲述顺序"}
    record = review_record(selected)
    question = record["question"]
    question["prompt"] = "量化结果之后，时间与来源说明如何组织？"
    question["options"][0].update(label="先时间后来源",
        description="量化结果仍优先；随后先讲既定时间再讲来源，两部分均保留")
    question["options"][1].update(label="先来源后时间",
        description="量化结果仍优先；随后先讲来源再讲既定时间，两部分均保留")
    decision = fixed_decision(question="整篇报告采用什么主线？", answer="优先展示量化结果")
    decision["selected_options"][0]["description"] = "全文先展示量化结果，其后解释背景，所有必需内容均保留"
    primary = check_result(unit_ids=review_units(), option_labels={
        item["id"]: item["label"] for item in question["options"]})
    certificate = primary["decision_spaces"]["q0_space"]
    certificate.update(relation="confirmed_choice_competes", decision_ids=["decision_1"])
    certificate["options"]["a"].update(fixed_id="decision_1", effect="preserves_rule")
    certificate["options"]["b"].update(fixed_id="decision_1", effect="unrelated")
    proof = {"unresolved_axis": record["question"]["prompt"],
        "relation": "same_unresolved_axis", "decision_ids": [],
        "options": {item["id"]: {"evidence": item["label"], "role": "new_method",
            "fixed_id": "none", "action_span": "\n".join(item[field] for field in ("label", "description", "tradeoff")
                if isinstance(item.get(field), str) and item[field].strip())} for item in question["options"]}}
    return record, decision, primary, proof


def scoped_facts(record, decision, option_index=0, **changes):
    option = record["question"]["options"][option_index]
    result = {"fixed_rule_type": "confirmed_method_or_priority",
        "fixed_object": decision["answer"], "candidate_object": record["question"]["prompt"],
        "fixed_evidence": decision["answer"], "candidate_evidence": option["description"],
        "candidate_behavior": option["description"], "scope_relation": "within_original_object",
        "adds_action_precondition": "no", "changes_required_action": "no",
        "excludes_required_content_or_scope": "no", "changes_confirmed_method_or_priority": "no"}
    result.update(changes)
    return result


async def run_case(record, decision, primary, proof, focused):
    if not focused:
        # None fixtures exercise the legacy certificate gate. They never gain
        # production new-axis authority or permission to override a negative.
        coordinator, service = setup([primary], check_consistency=True)
        result = await prepare(coordinator, stage="writing", kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
        return result, service
    # The retained helper's grounding contract is tested directly. Production
    # List proofs use the complete matrix and never call this subset helper.
    coordinator, service = setup(focused, check_consistency=True)
    fixed = coordinator._stage_authority([decision], {})[0]
    try:
        await coordinator._recheck_negative_decision_relation(question=record["question"],
            certificate=primary["decision_spaces"]["q0_space"], fixed=fixed,
            context={"selected_topic": record["topic"], "ancestor_conditions": record["ancestor_conditions"]},
            creation_model=None, creation_api_key=None, creation_base_url=None)
    except _StageConsistencyError as error:
        if error.code == "confirmed_decision_conflict":
            return {"review": "reject", "codes": [error.code], "decision_ids": error.decision_ids}, service
        raise
    return {"review": "pass", "codes": [], "decision_ids": []}, service


@pytest.mark.asyncio
async def test_relation_only_false_negative_checks_each_actual_option_against_full_scoped_rule():
    record, decision, primary, proof = scenario()
    before = copy.deepcopy((record, decision, primary, proof))
    result, service = await run_case(record, decision, primary, [proof], [
        scoped_facts(record, decision), scoped_facts(record, decision, 1)])
    assert result == {"review": "pass", "codes": [], "decision_ids": []}
    assert [call["num_predict"] for call in service.model_calls] == [800, 800]
    assert (record, decision, primary, proof) == before
    for index in (0, 1):
        focused = json.loads(service.prompts[index])
        assert focused["fixed_decision"]["question"] == decision["question"]
        assert focused["fixed_decision"]["answer"] == decision["answer"]
        assert focused["fixed_decision"]["selected_options"][0]["description"] == decision["selected_options"][0]["description"]
        assert focused["candidate_alternative"]["option"]["id"] == record["question"]["options"][index]["id"]
        assert focused["trusted_context"]["selected_topic"] == record["topic"]
        assert "不应向checker复制的长来源" not in service.prompts[index]
        schema = service.model_calls[index]["json_schema"]["properties"]
        assert schema["fixed_object"]["enum"] == [decision["answer"]]
        assert schema["candidate_object"]["enum"] == [record["question"]["prompt"], record["question"]["options"][index]["label"]]
        assert decision["question"] not in schema["fixed_evidence"]["enum"]


@pytest.mark.parametrize("fact,kind,behavior", [
    ("changes_confirmed_method_or_priority", "confirmed_method_or_priority", "把全文量化主线换为技术过程"),
    ("changes_confirmed_method_or_priority", "confirmed_method_or_priority", "把已确认原表行序换为另一排序"),
    ("excludes_required_content_or_scope", "required_content_or_scope", "只保留收入而排除必需支出"),
    ("adds_action_precondition", "conditional_action", "需要累计两次瑕疵才停止出库"),
    ("changes_required_action", "conditional_action", "发现瑕疵仍继续出库"),
])
@pytest.mark.asyncio
async def test_scoped_recheck_cannot_erase_real_priority_method_content_trigger_or_action_change(fact, kind, behavior):
    record, decision, primary, proof = scenario()
    # Simulate a false primary pass and false independent new-axis verdict.
    # The targeted comparison must still reject the actual changed alternative.
    record["question"]["options"][1]["description"] = behavior
    focused = scoped_facts(record, decision, 1, fixed_rule_type=kind,
        scope_relation="same_object", **{fact: "yes"})
    result, service = await run_case(record, decision, primary, [proof], [
        scoped_facts(record, decision), focused])
    assert result == {"review": "reject", "codes": ["confirmed_decision_conflict"], "decision_ids": ["decision_1"]}
    assert len(service.model_calls) == 2


@pytest.mark.parametrize("change", [
    {"scope_relation": "unknown"}, {"changes_confirmed_method_or_priority": "unknown"},
    {"adds_action_precondition": "unknown"}, {"changes_required_action": "unknown"},
    {"excludes_required_content_or_scope": "unknown"},
])
@pytest.mark.asyncio
async def test_uncertain_scoped_fact_never_overrides_negative_relation(change):
    record, decision, primary, proof = scenario()
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await run_case(record, decision, primary, [proof], [scoped_facts(record, decision, **change)])


@pytest.mark.parametrize("change", [
    {"fixed_evidence": "旧规则中不存在的文字"}, {"candidate_evidence": "另一个选项的行为"},
    {"fixed_object": ""}, {"candidate_object": "文" * 81},
    {"candidate_behavior": "文" * 161}, {"extra": "未绑定字段"},
    {"fixed_rule_type": "untrusted"}, {"changes_confirmed_method_or_priority": []},
])
@pytest.mark.asyncio
async def test_malformed_scope_or_unbound_evidence_fails_closed(change):
    record, decision, primary, proof = scenario()
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_case(record, decision, primary, [proof], [scoped_facts(record, decision, **change)])


@pytest.mark.asyncio
async def test_candidate_scope_cannot_become_old_rule_even_when_checker_claims_a_change():
    record, decision, primary, proof = scenario()
    contaminated = scoped_facts(record, decision,
        fixed_object=record["question"]["prompt"], fixed_evidence=decision["question"],
        changes_confirmed_method_or_priority="yes")
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_case(record, decision, primary, [proof], [contaminated])


@pytest.mark.asyncio
async def test_question_alone_never_proves_the_effective_selected_rule():
    record, decision, primary, proof = scenario()
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_case(record, decision, primary, [proof], [
            scoped_facts(record, decision, fixed_evidence=decision["question"])])


@pytest.mark.asyncio
async def test_current_selected_description_remains_complete_rule_evidence():
    record, decision, primary, proof = scenario()
    evidence = decision["selected_options"][0]["description"]
    result, service = await run_case(record, decision, primary, [proof], [
        scoped_facts(record, decision, fixed_evidence=evidence),
        scoped_facts(record, decision, 1, fixed_evidence=evidence)])
    assert result["review"] == "pass"
    assert service.model_calls[0]["json_schema"]["properties"]["fixed_evidence"]["enum"] == [decision["answer"], evidence]


@pytest.mark.asyncio
async def test_manually_edited_answer_cannot_restore_obsolete_selected_description():
    record, decision, primary, proof = scenario()
    decision["manually_edited"] = True
    decision["answer"] = "先展示当前用户指定的成果，再说明背景"
    old_description = decision["selected_options"][0]["description"]
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_case(record, decision, primary, [proof], [
            scoped_facts(record, decision, fixed_evidence=old_description)])
    result, service = await run_case(record, decision, primary, [proof], [
        scoped_facts(record, decision), scoped_facts(record, decision, 1)])
    assert result["review"] == "pass"
    assert service.model_calls[0]["json_schema"]["properties"]["fixed_evidence"]["enum"] == [decision["answer"]]


@pytest.mark.parametrize("proof_kind", ["none", "missing", "fixed_role", "wrong_evidence", "fragment", "wrong_id"])
@pytest.mark.asyncio
async def test_missing_or_unbound_independent_new_axis_cannot_overrule_primary(proof_kind):
    record, decision, primary, proof = scenario()
    proofs = [proof]
    if proof_kind == "none": proofs = None
    elif proof_kind == "missing": proofs = []
    elif proof_kind == "fixed_role": proof["options"]["a"]["role"] = "fixed_requirement"
    elif proof_kind == "wrong_evidence": proof["options"]["a"]["evidence"] = record["question"]["options"][1]["label"]
    elif proof_kind == "fragment": proof["options"]["a"]["action_span"] = "先时间"
    elif proof_kind == "wrong_id": proof["options"]["a"]["fixed_id"] = "decision_1"
    assert not BrainstormStageCoordinator._verified_new_axis(
        {"review_questions": [record]}, proofs, "q0", record["question"])
    result, service = await run_case(record, decision, primary, proofs, [])
    assert result["review"] == "reject" and len(service.model_calls) == 1


@pytest.mark.parametrize("reason", ["unit_conflict", "unit_scope", "changed_effect", "unknown_effect", "invalid_identity"])
@pytest.mark.asyncio
async def test_local_negative_or_invalid_primary_cannot_enter_relation_only_override(reason):
    record, decision, primary, proof = scenario()
    certificate = primary["decision_spaces"]["q0_space"]
    if reason == "unit_conflict": primary["checks"]["q0_o1"] = "confirmed_decision_conflict:decision_1"
    elif reason == "unit_scope": primary["checks"]["q0_o1"] = "topic_scope"
    elif reason == "changed_effect": certificate["options"]["b"]["effect"] = "changes_required_set"
    elif reason == "unknown_effect": certificate["options"]["b"]["effect"] = "unknown"
    else: certificate["options"]["a"]["option_evidence"] = "未绑定选项"
    if reason in {"unknown_effect", "invalid_identity"}:
        with pytest.raises(BrainstormGenerationError):
            await run_case(record, decision, primary, [proof], [])
    else:
        result, service = await run_case(record, decision, primary, [proof], [])
        assert result["review"] == "reject" and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_retained_scoped_comparison_propagates_cancellation():
    record, decision, primary, proof = scenario()
    class SlowScopeService(StubCreationService):
        cancelled = False
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            self.prompts.append(kwargs["user_prompt"])
            if kwargs["num_predict"] == 800:
                try:
                    await asyncio.sleep(1)
                finally:
                    self.cancelled = True
            else:
                yield json.dumps(primary, ensure_ascii=False)
    service = SlowScopeService([])
    coordinator = BrainstormStageCoordinator(service)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(coordinator._recheck_negative_decision_relation(
            question=record["question"], certificate=primary["decision_spaces"]["q0_space"],
            fixed=coordinator._stage_authority([decision], {})[0], context={"selected_topic": record["topic"]},
            creation_model=None, creation_api_key=None, creation_base_url=None), 0.04)
    assert service.cancelled
    assert [call["num_predict"] for call in service.model_calls] == [800]
