"""Initial brainstorming has an empty confirmation set, not missing authority."""
import copy
import json

import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import candidate, fixed_decision, prepare, setup
from tests.test_brainstorm_stages import plan, topic


def schema_for(fixed):
    question = {"id": "root", "prompt": "本周报应重点解释什么？", "options": [
        {"id": "a", "label": "解释成本", "description": "先说明成本构成"}]}
    context = {"question": question, "selected_topic": topic(), "effective_goal": "比较汇报重点",
        "ancestor_conditions": []}
    unit = {"id": "g0_0", "kind": "next_goal", "text": "比较成本说明方法",
        "branch_option_id": "a", "fixed_compatibility_required": True}
    task = {"fixed_decisions": fixed, "cleared_constraints": [], "user_constraints": {"root_request": "生成周报"}}
    return Coordinator._bound_scope_schema(unit, context, task,
        {"pass": ("none", []), "confirmed_decision_conflict:decision_1": ("confirmed_decision_conflict", ["decision_1"])})


def response(schema):
    values = {}
    preferred = {"scope": "within", "parent": "preserves", "future_fixed": "pass"}
    for field, prop in schema["properties"].items():
        values[field] = preferred.get(field, "no") if preferred.get(field, "no") in prop["enum"] else prop["enum"][0]
    return values


def test_empty_user_confirmations_cannot_be_misreported_as_missing_context():
    schema = schema_for([])
    assert schema["properties"]["future_fixed"]["enum"] == ["not_applicable"]
    checked = response(schema)
    assert Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema}) == {"g0_0": "pass"}
    original = copy.deepcopy(checked)
    checked["future_fixed"] = "insufficient_context"
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema})
    assert original["future_fixed"] == "not_applicable"


def test_real_confirmation_still_requires_full_future_rule_check():
    fixed = Coordinator._stage_authority([fixed_decision()], {})[0]
    schema = schema_for(fixed)
    assert schema["properties"]["future_fixed"]["enum"] == ["pass", "insufficient_context", "confirmed_decision_conflict:decision_1"]
    checked = response(schema)
    checked["future_fixed"] = "confirmed_decision_conflict:decision_1"
    assert Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema}) == {"g0_0": "confirmed_decision_conflict:decision_1"}
    checked["future_fixed"] = "insufficient_context"
    with pytest.raises(_StageConsistencyError) as error:
        Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema})
    assert error.value.failed_checks == [{"unit_id": "g0_0", "field": "future_fixed"}]


def test_excluded_decision_does_not_create_a_confirmation_but_keeps_exclusion_gate():
    schema = schema_for([{"decision_id": "decision_1", "answer_source": "user_excluded"}])
    assert schema["properties"]["future_fixed"]["enum"] == ["not_applicable"]
    checked = response(schema)
    checked["exclusion"] = "violates:decision_1"
    assert Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema}) == {"g0_0": "exclusion_conflict:decision_1"}


def test_unknown_fact_remains_blocking_and_feedback_identifies_field_without_inventing_facts():
    schema = schema_for([])
    checked = response(schema)
    checked["claims_hypothetical_path_confirmed"] = "unknown"
    with pytest.raises(_StageConsistencyError) as error:
        Coordinator._bound_scope_verdicts({"g0_0": checked}, {"g0_0": schema})
    feedback = Coordinator._consistency_repair_feedback(error.value, None, [])
    assert feedback["failed_checks"] == [{"unit_id": "g0_0", "field": "claims_hypothetical_path_confirmed"}]
    assert feedback["fixed_decisions"] == [] and feedback["rejected_candidate"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan", "topic_plan"])
async def test_planning_reads_current_authority_without_recalling_old_report_periods(kind):
    from tests.brainstorm_axis_fixtures import axis_plan_raw
    value = axis_plan_raw("direction")
    coordinator, service = setup([value])
    calls = []
    coordinator._retrieve_memory = lambda *args: calls.append(args) or {"status": "found", "sources": []}
    kwargs = {"kind": kind}
    if kind == "topic_plan":
        kwargs["root_limit"] = 4
    await prepare(coordinator, stage="direction", **kwargs)
    assert calls == []
    if kind == "topic_plan":
        assert service.model_calls == [] and service.prompts == []
        return
    generated = json.loads(service.prompts[0])
    assert generated["memory"] == {"status": "skipped", "sources": []}
    assert generated["confirmed_context"] == []


@pytest.mark.asyncio
async def test_actual_node_still_uses_source_recall():
    coordinator, service = setup([candidate(2)])
    calls = []
    coordinator._retrieve_memory = lambda *args: calls.append(args) or {"status": "skipped", "sources": []}
    from tests.test_brainstorm_stage_nodes import node
    await prepare(coordinator, node=node(2))
    assert len(calls) == 1
