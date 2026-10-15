"""Whole-choice novelty cannot be inferred from individual compatibility."""
import copy
import hashlib
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_nodes import candidate, fixed_decision, node, setup
from tests.test_brainstorm_stages import topic
from tests.test_creation_brainstorm import StubCreationService


def complete_action(option):
    return "\n".join(option[field] for field in ("label", "description", "tradeoff")
        if isinstance(option.get(field), str) and option[field].strip())


def option_digest(option):
    return hashlib.sha256(json.dumps(option, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def fixture():
    result = {"question": {"prompt": "选择记录组织方式", "options": [
        {"id": "a", "label": "按对象组织记录", "description": "按对象记录全部既定检查结果"},
        {"id": "b", "label": "按时间组织记录", "description": "按检查时间记录全部既定检查结果"}]}, "next_goals": []}
    verdict = {"question_evidence": result["question"]["prompt"], "question_role_binding": "new_choice",
        "options": {identifier: {"evidence": option["label"],
        "role_binding": "new_method", "action_span": complete_action(option)}
        for identifier, option in [(x["id"],x) for x in result["question"]["options"]]},
        "relation": "same_unresolved_axis", "decision_ids": []}
    task = {"fixed_decisions": BrainstormStageCoordinator._stage_authority([fixed_decision()],{})[0],
        "ancestor_conditions": [], "goal": "比较记录组织方法"}
    return result, verdict, task


async def check(result, verdict, task):
    coordinator, service = setup([verdict])
    await BrainstormStageCoordinator._check_new_decision_space(coordinator,
        candidate=result, task=task, topic=topic(), creation_model=None,
        creation_api_key=None, creation_base_url=None)
    return service


@pytest.mark.asyncio
async def test_new_methods_are_bound_to_actual_option_text_and_keep_all_fixed_authority():
    result, verdict, task = fixture()
    before = copy.deepcopy((result,task))
    service = await check(result,verdict,task)
    payload = json.loads(service.prompts[0])
    assert payload["fixed_decisions"] == task["fixed_decisions"]
    assert payload["candidate"]["question"] == result["question"]
    assert payload["option_bindings"] == {option["id"]: {"option_index": index,
        "option_sha256": option_digest(option)} for index, option in enumerate(result["question"]["options"])}
    assert (result,task) == before and len(service.model_calls) == 1
    assert service.model_calls[0]["temperature"] == 0.0
    schema = service.model_calls[0]["json_schema"]
    assert set(schema["properties"]) == {"question_evidence", "question_role_binding", "options", "relation", "decision_ids"}
    assert schema["properties"]["question_evidence"] == {"type": "string", "enum": [result["question"]["prompt"]]}
    assert schema["properties"]["question_role_binding"]["enum"] == ["new_choice", "outside_current_scope",
        "unknown", "reconfirms_fixed:decision_1"]
    assert schema["additionalProperties"] is False
    for option in result["question"]["options"]:
        option_schema = schema["properties"]["options"]["properties"][option["id"]]
        assert set(option_schema["properties"]) == {"evidence", "role_binding", "action_span"}
        assert set(option_schema["required"]) == {"evidence", "role_binding", "action_span"}
        assert option_schema["additionalProperties"] is False
        assert option_schema["properties"]["role_binding"]["enum"] == ["new_method", "unknown",
            "fixed_requirement:decision_1", "reselects_fixed:decision_1", "incompatible:decision_1"]
        assert option_schema["properties"]["action_span"] == {"type": "string", "enum": [complete_action(option)]}


@pytest.mark.parametrize("relation,role", [("fixed_choices_only","fixed_requirement"),
    ("different_required_tasks","fixed_requirement"), ("same_unresolved_axis","reselects_fixed"),
    ("same_unresolved_axis","incompatible")])
@pytest.mark.asyncio
async def test_joint_fixed_tasks_or_fixed_choices_never_gain_new_space_from_global_pass(relation,role):
    result, verdict, task = fixture()
    verdict["relation"] = relation; verdict["decision_ids"] = ["decision_1"]
    verdict["options"]["a"]["role_binding"] = role + ":decision_1"
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision"):
        await check(result,verdict,task)


@pytest.mark.parametrize("mutation", [
    lambda v: v["options"]["b"].update(evidence="虚假的其他选项"),
    lambda v: v["options"]["b"].update(action_span="杜撰不存在的新增实施办法"),
    lambda v: v["options"]["b"].update(action_span="按时间"),
    lambda v: v["options"]["b"].update(role_binding="fixed_requirement:unknown"),
    lambda v: v["options"]["b"].update(role_binding="new_method:decision_1"),
    lambda v: v["options"]["b"].update(role_binding="fixed_requirement:none"),
    lambda v: v["options"]["b"].update(role_binding="fixed_requirement:decision_1:extra"),
    lambda v: v["options"]["b"].update(role_binding="new_method "),
    lambda v: v["options"]["b"].update(role_binding=["new_method"]),
    lambda v: v["options"]["b"].pop("role_binding"),
    lambda v: v["options"]["b"].update(role="new_method", fixed_id="decision_1"),
    lambda v: v["options"].pop("b"),
    lambda v: v["options"].update(fake=copy.deepcopy(v["options"]["a"])),
    lambda v: v.update(decision_ids=["unknown"]),
    lambda v: v.update(relation="unknown"),
    lambda v: v["options"]["b"].update(role_binding="unknown"),
])
@pytest.mark.asyncio
async def test_unbound_missing_or_unknown_relationship_certificate_cannot_release_candidate(mutation):
    result, verdict, task = fixture(); before=copy.deepcopy(result)
    mutation(verdict)
    with pytest.raises(BrainstormGenerationError):
        await check(result,verdict,task)
    assert result == before


@pytest.mark.asyncio
async def test_review_uses_real_topic_and_ancestors_without_converting_candidates_to_decisions():
    result, verdict, task = fixture()
    record={"question":result["question"],"topic":topic(),"ancestor_conditions":[{"option_label":"父候选"}]}
    service = await check({"review_questions":[record]},verdict,task)
    assert json.loads(service.prompts[0])["candidate"] == record
    assert json.loads(service.prompts[0])["fixed_decisions"] == task["fixed_decisions"]


@pytest.mark.asyncio
async def test_bound_fixed_role_cannot_be_hidden_by_false_same_axis_without_overall_references():
    result, verdict, task = fixture()
    verdict["options"]["a"]["role_binding"] = "fixed_requirement:decision_1"
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_fixed_role_without_bound_current_rule_fails_closed():
    result, verdict, task = fixture()
    verdict["options"]["a"]["role_binding"] = "fixed_requirement:none"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_verified_certificate_is_returned_only_after_every_actual_option_passes():
    result, verdict, task = fixture()
    coordinator, _ = setup([verdict])
    checked = await BrainstormStageCoordinator._check_new_decision_space(coordinator,
        candidate=result, task=task, topic=topic(), creation_model=None,
        creation_api_key=None, creation_base_url=None)
    expected = {"unresolved_axis": result["question"]["prompt"], "options": copy.deepcopy(verdict["options"]),
        "relation": verdict["relation"], "decision_ids": copy.deepcopy(verdict["decision_ids"])}
    for option in expected["options"].values():
        del option["role_binding"]
        option.update(role="new_method", fixed_id="none")
    assert checked == [expected]
    checked[0]["options"]["a"]["role"] = "fixed_requirement"
    checked[0]["decision_ids"].append("decision_1")
    assert verdict["options"]["a"]["role_binding"] == "new_method"
    assert set(verdict["options"]["a"]) == {"evidence", "role_binding", "action_span"}
    assert verdict["decision_ids"] == []
    assert "unresolved_axis" not in verdict


@pytest.mark.parametrize("role,code", [("fixed_requirement", "confirmed_decision_reopened"),
    ("reselects_fixed", "confirmed_decision_reopened"), ("incompatible", "confirmed_decision_conflict")])
@pytest.mark.asyncio
async def test_last_option_bound_fixed_role_overrides_global_same_axis_with_precise_authority(role, code):
    result, verdict, task = fixture()
    verdict["options"]["b"]["role_binding"] = role + ":decision_1"
    with pytest.raises(BrainstormGenerationError, match=code) as error:
        await check(result, verdict, task)
    assert error.value.decision_ids == ["decision_1"]


@pytest.mark.asyncio
async def test_unknown_role_binding_is_not_a_success_certificate():
    result, verdict, task = fixture()
    verdict["options"]["b"]["role_binding"] = "unknown"
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_model_schema_uses_only_current_fixed_authority_not_candidate_ids():
    result, verdict, task = fixture()
    task["fixed_decisions"][0]["decision_id"] = "decision_4"
    result["question"]["options"][0]["id"] = "decision_99"
    verdict["options"]["decision_99"] = verdict["options"].pop("a")
    service = await check(result, verdict, task)
    properties = service.model_calls[0]["json_schema"]["properties"]["options"]["properties"]
    for option_schema in properties.values():
        assert option_schema["properties"]["role_binding"]["enum"] == ["new_method", "unknown",
            "fixed_requirement:decision_4", "reselects_fixed:decision_4", "incompatible:decision_4"]
    assert service.model_calls[0]["json_schema"]["properties"]["question_role_binding"]["enum"] == [
        "new_choice", "outside_current_scope", "unknown", "reconfirms_fixed:decision_4"]
    verdict["options"]["b"]["role_binding"] = "incompatible:decision_99"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_no_fixed_decisions_has_no_fabricated_fixed_role_binding():
    result, verdict, task = fixture()
    task["fixed_decisions"] = []
    service = await check(result, verdict, task)
    properties = service.model_calls[0]["json_schema"]["properties"]["options"]["properties"]
    assert all(value["properties"]["role_binding"]["enum"] == ["new_method", "unknown"]
        for value in properties.values())
    assert service.model_calls[0]["json_schema"]["properties"]["question_role_binding"]["enum"] == [
        "new_choice", "outside_current_scope", "unknown"]
    verdict["options"]["b"]["role_binding"] = "fixed_requirement:decision_1"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.parametrize("decision_id", ["decision_1", "decision_4"])
@pytest.mark.asyncio
async def test_old_independent_role_and_fixed_id_wire_is_rejected(decision_id):
    result, verdict, task = fixture()
    task["fixed_decisions"][0]["decision_id"] = decision_id
    for option in verdict["options"].values():
        del option["role_binding"]
        option.update(role="new_method", fixed_id=decision_id)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_unknown_first_option_does_not_skip_invalid_last_option_binding():
    result, verdict, task = fixture()
    verdict["options"]["a"]["role_binding"] = "unknown"
    verdict["options"]["b"]["role_binding"] = "incompatible:invented_decision"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.asyncio
async def test_all_24_authority_bindings_are_enumerated_and_last_real_id_is_preserved_on_rejection():
    result, verdict, task = fixture()
    original = task["fixed_decisions"][0]
    task["fixed_decisions"] = [{**copy.deepcopy(original), "decision_id": "decision_{}".format(index)}
        for index in range(1, 25)]
    service = await check(result, verdict, task)
    option_schema = service.model_calls[0]["json_schema"]["properties"]["options"]["properties"]["b"]
    bindings = option_schema["properties"]["role_binding"]["enum"]
    assert len(bindings) == 74 and len(set(bindings)) == 74
    assert set(bindings) == {"new_method", "unknown"}.union({"{}:decision_{}".format(role, index)
        for role in ("fixed_requirement", "reselects_fixed", "incompatible") for index in range(1, 25)})
    question_bindings = service.model_calls[0]["json_schema"]["properties"]["question_role_binding"]["enum"]
    assert len(question_bindings) == 27 and set(question_bindings) == {"new_choice", "outside_current_scope", "unknown"}.union(
        {"reconfirms_fixed:decision_{}".format(index) for index in range(1, 25)})
    verdict["options"]["b"]["role_binding"] = "incompatible:decision_24"
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await check(result, verdict, task)
    assert error.value.decision_ids == ["decision_24"]


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(question_evidence="题干中不存在的缺失项样式与暂停规则"),
    lambda value: value.update(question_evidence="记录组织"),
    lambda value: value.update(question_evidence=["选择记录组织方式"]),
    lambda value: value.pop("question_evidence"),
    lambda value: value.update(question_role_binding="reconfirms_fixed:made_up"),
    lambda value: value.update(question_role_binding="reconfirms_fixed:decision_1:extra"),
    lambda value: value.update(question_role_binding="new_choice:decision_1"),
    lambda value: value.update(question_role_binding="outside_current_scope:decision_1"),
    lambda value: value.update(question_role_binding=["new_choice"]),
    lambda value: value.pop("question_role_binding"),
    lambda value: value.update(unresolved_axis="缺失项样式与暂停规则"),
])
@pytest.mark.asyncio
async def test_unbound_question_evidence_invalid_role_or_old_free_axis_cannot_release_candidate(mutation):
    result, verdict, task = fixture()
    before = copy.deepcopy((result, task))
    mutation(verdict)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)
    assert (result, task) == before


@pytest.mark.asyncio
async def test_previous_free_unresolved_axis_wire_shape_is_not_accepted_as_a_question_certificate():
    result, verdict, task = fixture()
    del verdict["question_evidence"]
    del verdict["question_role_binding"]
    verdict["unresolved_axis"] = "题干中不存在的缺失项样式与暂停规则"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.parametrize("binding,code", [("outside_current_scope", "topic_scope"),
    ("unknown", "insufficient_context")])
@pytest.mark.asyncio
async def test_question_role_cannot_be_overridden_by_all_new_method_options_and_same_axis(binding, code):
    result, verdict, task = fixture()
    verdict["question_role_binding"] = binding
    with pytest.raises(BrainstormGenerationError, match=code):
        await check(result, verdict, task)


@pytest.mark.parametrize("decision_id", ["decision_2", "decision_12"])
@pytest.mark.asyncio
async def test_actual_fixed_period_question_cannot_be_reinterpreted_as_an_invented_organization_axis(decision_id):
    result, verdict, task = fixture()
    result["question"]["prompt"] = "锁定2026-08-17至23日Asia/Shanghai周期数据"
    result["question"]["options"] = [
        {"id": "a", "label": "采用已确认周期", "description": "采用已确认的2026-08-17至23日Asia/Shanghai周期数据"},
        {"id": "b", "label": "专业来源与灰色标识", "description": "优先专业来源，并使用已确认的灰色缺失数据标识"}]
    task["fixed_decisions"][0].update(decision_id=decision_id,
        answer="周期固定为2026-08-17至23日Asia/Shanghai")
    verdict["question_evidence"] = result["question"]["prompt"]
    verdict["question_role_binding"] = "reconfirms_fixed:" + decision_id
    verdict["options"] = {option["id"]: {"evidence": option["label"], "action_span": complete_action(option),
        "role_binding": "new_method"} for option in result["question"]["options"]}
    # Even the former all-new/same-axis false positive cannot waive the bound
    # question's fixed-object certificate or replace it with another topic.
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened") as error:
        await check(result, verdict, task)
    assert error.value.decision_ids == [decision_id]


@pytest.mark.asyncio
async def test_actual_post_quantification_organization_question_remains_a_new_choice():
    result, verdict, task = fixture()
    result["question"]["prompt"] = "量化结果之后，周期与来源说明如何组织？"
    result["question"]["options"] = [
        {"id": "a", "label": "先说明周期再说明来源", "description": "量化结果展示后先说明固定周期，再说明全部既定来源，完整保留已确认内容"},
        {"id": "b", "label": "先说明来源再说明周期", "description": "量化结果展示后先说明全部既定来源，再说明固定周期，完整保留已确认内容"}]
    verdict["question_evidence"] = result["question"]["prompt"]
    verdict["options"] = {option["id"]: {"evidence": option["label"], "action_span": complete_action(option),
        "role_binding": "new_method"} for option in result["question"]["options"]}
    coordinator, service = setup([verdict])
    checked = await BrainstormStageCoordinator._check_new_decision_space(coordinator,
        candidate=result, task=task, topic=topic(), creation_model=None,
        creation_api_key=None, creation_base_url=None)
    assert checked[0]["unresolved_axis"] == result["question"]["prompt"]
    assert set(checked[0]) == {"unresolved_axis", "options", "relation", "decision_ids"}
    assert all(value["role"] == "new_method" and value["fixed_id"] == "none"
        for value in checked[0]["options"].values())
    assert len(service.model_calls) == 1 and service.model_calls[0]["temperature"] == 0.0


@pytest.mark.parametrize("question_role", ["reconfirms_fixed:decision_1", "outside_current_scope", "unknown"])
@pytest.mark.asyncio
async def test_question_failure_does_not_skip_invalid_last_option_binding(question_role):
    result, verdict, task = fixture()
    verdict["question_role_binding"] = question_role
    verdict["options"]["b"]["role_binding"] = "incompatible:made_up"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)


@pytest.mark.parametrize("reconfirms", [False, True])
@pytest.mark.asyncio
async def test_system_examples_do_not_echo_actual_acceptance_question_or_option_labels(reconfirms):
    result, verdict, task = fixture()
    if reconfirms:
        result["question"]["prompt"] = "锁定2026-08-17至23日Asia/Shanghai周期数据"
        labels = ["采用已确认周期", "专业来源与灰色标识"]
        verdict["question_role_binding"] = "reconfirms_fixed:decision_1"
    else:
        result["question"]["prompt"] = "量化结果之后，周期与来源说明如何组织？"
        labels = ["先交代周期再介绍来源", "先介绍来源再交代周期"]
    for option, label in zip(result["question"]["options"], labels):
        option["label"] = label
    verdict["question_evidence"] = result["question"]["prompt"]
    verdict["options"] = {option["id"]: {"evidence": option["label"], "action_span": complete_action(option),
        "role_binding": "new_method"} for option in result["question"]["options"]}
    coordinator, service = setup([verdict])
    async def run():
        return await BrainstormStageCoordinator._check_new_decision_space(coordinator,
            candidate=result, task=task, topic=topic(), creation_model=None,
            creation_api_key=None, creation_base_url=None)
    if reconfirms:
        with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened"):
            await run()
    else:
        await run()
    system = service.model_calls[0]["system_prompt"]
    assert all(text not in system for text in [result["question"]["prompt"]] + labels)
    assert "设备与材料说明" in system and "锁定已确认场地" in system
    assert json.loads(service.prompts[0])["candidate"]["question"] == result["question"]
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_internal_correct_roles_cannot_replay_a_different_question_prompt():
    result, verdict, task = fixture()
    certificate = {"unresolved_axis": "另一个问题的组织方式", "relation": "same_unresolved_axis",
        "decision_ids": [], "options": {option["id"]: {"evidence": option["label"],
            "role": "new_method", "fixed_id": "none", "action_span": complete_action(option)}
            for option in result["question"]["options"]}}
    service = StubCreationService([])
    coordinator = BrainstormStageCoordinator(service)
    async def replay(**kwargs):
        return [copy.deepcopy(certificate)]
    coordinator._check_new_decision_space = replay
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await coordinator._check_stage_consistency(stage="detail", kind="node", topic=topic(),
            task=task, result=result, other_topics=[], creation_model=None,
            creation_api_key=None, creation_base_url=None)
    assert service.model_calls == []
    certificate["unresolved_axis"] = result["question"]["prompt"]
    assert coordinator._verified_new_axis(result, [certificate], "q0", result["question"]) is True
    certificate["unresolved_axis"] = result["question"]["prompt"] + " "
    assert coordinator._verified_new_axis(result, [certificate], "q0", result["question"]) is False


@pytest.mark.asyncio
async def test_real_multi_question_review_unknown_second_question_never_returns_first_question_pass():
    result, verdict, task = fixture()
    second = copy.deepcopy(result["question"])
    second["prompt"] = "第二份检查记录采用哪种组织方式？"
    unknown = copy.deepcopy(verdict)
    unknown.update(question_evidence=second["prompt"], question_role_binding="unknown")
    records = [{"question": question, "topic": topic(), "ancestor_conditions": []}
        for question in (result["question"], second)]
    service = StubCreationService([json.dumps(item, ensure_ascii=False) for item in (verdict, unknown)])
    coordinator = BrainstormStageCoordinator(service)
    before = copy.deepcopy(records)
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await coordinator._review_preparation(stage="detail", root_request="组织检查记录", decisions=[],
            brief_edits={}, plan={"topics": [topic()]}, limits=coordinator.DEFAULT_LIMITS,
            questions=records, creation_model=None, creation_api_key=None, creation_base_url=None)
    assert len(service.model_calls) == 2
    assert [json.loads(prompt)["candidate"]["question"]["prompt"] for prompt in service.prompts] == [
        result["question"]["prompt"], second["prompt"]]
    assert records == before


@pytest.mark.parametrize("field", ["label", "description", "tradeoff"])
@pytest.mark.asyncio
async def test_short_single_field_cannot_hide_description_that_only_repeats_a_fixed_requirement(field):
    result, verdict, task = fixture()
    option = result["question"]["options"][0]
    option.update(label="安排报告", description="只采用已确认周期，没有新增组织或实施办法",
        tradeoff="保持已确认时间边界")
    verdict["options"]["a"].update(evidence=option["label"], action_span=option[field])
    before = copy.deepcopy((result, task))
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await check(result, verdict, task)
    assert (result, task) == before
    # Complete evidence does not turn a fixed requirement into a new method:
    # the precise role/authority gate still blocks a false overall pass.
    verdict["options"]["a"].update(action_span=complete_action(option),
        role_binding="fixed_requirement:decision_1")
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_reopened") as error:
        await check(result, verdict, task)
    assert error.value.decision_ids == ["decision_1"]


@pytest.mark.parametrize("tradeoff", ["完整保留既定检查范围", "", "  ", None, 17, ["不是有效取舍文本"]])
@pytest.mark.asyncio
async def test_single_action_enum_contains_every_valid_actual_field_without_normalizing_its_text(tradeoff):
    result, verdict, task = fixture()
    option = result["question"]["options"][0]
    option.update(label="  按对象组织记录  ", description="首行\n次行全部保留", tradeoff=tradeoff)
    verdict["options"]["a"].update(evidence=option["label"], action_span=complete_action(option))
    service = await check(result, verdict, task)
    props = service.model_calls[0]["json_schema"]["properties"]["options"]["properties"]["a"]["properties"]
    assert props["action_span"]["enum"] == [complete_action(option)]
    assert json.loads(service.prompts[0])["candidate"]["question"]["options"][0] == option


@pytest.mark.parametrize("span", ["label", "description", "tradeoff", "no_tradeoff", "extra_space"])
def test_internal_correct_prompt_and_roles_require_the_complete_actual_action(span):
    result, verdict, _ = fixture()
    option = result["question"]["options"][0]
    option["tradeoff"] = "全部既定内容不变"
    proof = {"unresolved_axis": result["question"]["prompt"], "relation": "same_unresolved_axis",
        "decision_ids": [], "options": {actual["id"]: {"evidence": actual["label"],
            "role": "new_method", "fixed_id": "none", "action_span": complete_action(actual)}
            for actual in result["question"]["options"]}}
    assert BrainstormStageCoordinator._verified_new_axis(result, [proof], "q0", result["question"])
    proof["options"]["a"]["action_span"] = ("\n".join([option["label"], option["description"]])
        if span == "no_tradeoff" else complete_action(option) + " " if span == "extra_space" else option[span])
    assert not BrainstormStageCoordinator._verified_new_axis(result, [proof], "q0", result["question"])


class BoundActionService(StubCreationService):
    def __init__(self, mutate=None):
        super().__init__([])
        self.mutate = mutate

    async def _stream_direct_completion(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.model_calls.append(kwargs)
        properties = kwargs["json_schema"]["properties"]
        verdict = {"question_evidence": properties["question_evidence"]["enum"][0],
            "question_role_binding": "new_choice", "relation": "same_unresolved_axis", "decision_ids": [],
            "options": {identifier: {"evidence": schema["properties"]["evidence"]["enum"][0],
                "role_binding": "new_method", "action_span": schema["properties"]["action_span"]["enum"][0]}
                for identifier, schema in properties["options"]["properties"].items()}}
        if self.mutate:
            self.mutate(verdict)
        yield json.dumps(verdict, ensure_ascii=False, indent=2)


async def bound_check(result, task, service):
    return await BrainstormStageCoordinator._check_new_decision_space(BrainstormStageCoordinator(service),
        candidate=result, task=task, topic=topic(), creation_model=None,
        creation_api_key=None, creation_base_url=None)


@pytest.mark.parametrize("length,use_refs", [(10, False), (5000, True)])
@pytest.mark.asyncio
async def test_five_complete_options_adapt_output_budget_and_long_evidence_refs_preserve_full_input(length, use_refs):
    result, _, task = fixture()
    result["question"]["options"] = [{"id": "option_{}".format(index), "label": "组织办法{}".format(index),
        "description": "完整实际选项内容" * length, "tradeoff": "保留既定要求" * 3,
        "reference_ids": ["actual_source_{}".format(index)]} for index in range(5)]
    before = copy.deepcopy((result, task))
    service = BoundActionService()
    certificates = await bound_check(result, task, service)
    call = service.model_calls[0]
    payload = json.loads(service.prompts[0])
    assert payload["fixed_decisions"] == task["fixed_decisions"]
    assert payload["candidate"]["question"] == result["question"]
    assert 1200 < call["num_predict"] <= 4000 and call["temperature"] == 0.0
    assert len(service.model_calls) == 1 and (result, task) == before
    for index, option in enumerate(result["question"]["options"]):
        binding = payload["option_bindings"][option["id"]]
        assert binding == {"option_index": index, "option_sha256": option_digest(option)}
        properties = call["json_schema"]["properties"]["options"]["properties"][option["id"]]["properties"]
        assert properties["action_span"]["enum"] == [
            "option:" + binding["option_sha256"] if use_refs else complete_action(option)]
        assert certificates[0]["options"][option["id"]]["action_span"] == complete_action(option)
    assert BrainstormStageCoordinator._verified_new_axis(result, certificates, "q0", result["question"])


@pytest.mark.parametrize("mutation", [
    lambda verdict: verdict["options"]["a"].update(action_span=verdict["options"]["b"]["action_span"]),
    lambda verdict: verdict["options"]["a"].update(action_span="option:" + "0" * 64),
    lambda verdict: verdict["options"]["a"].update(action_span="按对象组织记录"),
    lambda verdict: verdict["options"]["a"].update(action_span=verdict["options"]["a"]["action_span"] + " "),
])
@pytest.mark.asyncio
async def test_long_action_reference_must_be_the_exact_program_bound_whole_actual_option(mutation):
    result, _, task = fixture()
    for option in result["question"]["options"]:
        option["description"] *= 1000
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await bound_check(result, task, BoundActionService(mutation))


@pytest.mark.asyncio
async def test_program_bound_action_reference_does_not_waive_unknown_semantic_role():
    result, _, task = fixture()
    for option in result["question"]["options"]:
        option["description"] *= 1000
    service = BoundActionService(lambda verdict: verdict["options"]["b"].update(role_binding="unknown"))
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await bound_check(result, task, service)
    assert all(schema["properties"]["action_span"]["enum"][0].startswith("option:")
        for schema in service.model_calls[0]["json_schema"]["properties"]["options"]["properties"].values())


@pytest.mark.asyncio
async def test_actual_option_change_during_checker_call_invalidates_its_complete_evidence_binding():
    result, _, task = fixture()
    service = BoundActionService(lambda verdict: result["question"]["options"][0].update(
        description="调用期间被改变的实际选项内容"))
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await bound_check(result, task, service)
    assert len(service.model_calls) == 1
