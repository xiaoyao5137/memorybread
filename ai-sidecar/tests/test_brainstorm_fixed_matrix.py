"""Every current choice inherits every effective confirmed rule."""
import asyncio
import copy
import hashlib
import json

import pytest

from creation.brainstorm import BrainstormGenerationError, BrainstormGenerationTimeout
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_nodes import (
    candidate, check_result, fixed_decision, node, prepare, review_record, review_units, setup,
)
from tests.test_creation_brainstorm import StubCreationService


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}
FACTS = ["adds_action_precondition", "changes_required_action",
    "excludes_required_content_or_scope", "changes_confirmed_method_or_priority"]
MATRIX_FACTS = FACTS + ["repeats_fixed_choice"]


def decisions(count=24):
    values = []
    for index in range(1, count + 1):
        value = fixed_decision(question="既定对象{}采用什么规则？".format(index),
            answer="保留既定对象{}的完整内容与方法".format(index))
        value["question_id"] = "fixed-{}".format(index)
        values.append(value)
    return values


def matrix(fixed, question):
    return {rule["decision_id"]: {"fixed_evidence": rule["answer"] or rule.get("excluded_topic", rule["question"]), "options": {
        option["id"]: [0] * 5 for option in question["options"]}} for rule in fixed}


def pair_checks(checked, question, *, fixed, clear=False, repeats=False):
    options = {option["id"]: option for option in question["options"]}
    authority = {rule["decision_id"]: rule for rule in fixed}
    pairs = [(identifier, option_id, MATRIX_FACTS[fact_index])
        for identifier, record in checked.items() for option_id, cell in record["options"].items()
        for fact_index, value in enumerate(cell) if value == 1 and (fact_index == 4 if repeats else fact_index < 4)]
    return {"pair_{}".format(index): {"fixed_evidence": BrainstormStageCoordinator._fixed_policy_evidence(authority[identifier]),
        "candidate_evidence": "\n".join(options[option_id][field] for field in ("label", "description", "tradeoff")
            if isinstance(options[option_id].get(field), str) and options[option_id][field].strip()),
        "fact_name": fact_name, "verdict": "no" if clear else "yes"}
        for index, (identifier, option_id, fact_name) in enumerate(pairs)}


def fixture(count=24, option_count=2):
    record = review_record()
    record["question"]["prompt"] = "在全部既定内容和方法不变的前提下，说明段落按什么顺序展开？"
    for index, option in enumerate(record["question"]["options"]):
        option.update(label="新增组织方法{}".format(index + 1),
            description="全部既定内容与方法不变，仅采用说明段落的局部组织方法{}".format(index + 1))
    for index in range(2, option_count):
        record["question"]["options"].append({"id": chr(ord("a") + index),
            "label": "新增组织方法{}".format(index + 1),
            "description": "全部既定内容与方法不变，仅采用说明段落的局部组织方法{}".format(index + 1)})
    original = decisions(count)
    fixed = BrainstormStageCoordinator._stage_authority(original, {})[0]
    return record, original, fixed


async def run_direct(fixed, record, responses):
    coordinator, service = setup(responses)
    await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
        context={"selected_topic": record["topic"], "node_goal": "比较说明段的局部组织方法",
            "ancestor_conditions": record["ancestor_conditions"]}, **MODEL)
    return service


@pytest.mark.parametrize("count,option_count", [(1, 2), (7, 2), (24, 2), (24, 5)])
@pytest.mark.asyncio
async def test_all_rules_and_all_actual_options_have_program_bound_matrix_cells(count, option_count):
    record, original, fixed = fixture(count, option_count)
    before = copy.deepcopy((record, original, fixed))
    class CompleteMatrixService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            response = matrix(payload["fixed_decisions"], payload["candidate_question"])
            yield json.dumps(response, ensure_ascii=False)
    service = CompleteMatrixService([])
    coordinator = BrainstormStageCoordinator(service)
    await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
        context={"selected_topic": record["topic"], "node_goal": "比较说明段的局部组织方法",
            "ancestor_conditions": record["ancestor_conditions"]}, **MODEL)
    assert (record, original, fixed) == before
    assert (count + 11) // 12 <= len(service.model_calls) <= count
    if option_count == 2:
        assert len(service.model_calls) == (count + 11) // 12
    covered = []
    offset = 0
    for index, prompt in enumerate(service.prompts):
        payload = json.loads(prompt)
        batch = fixed[offset:offset + len(payload["fixed_decisions"])]
        offset += len(batch)
        assert 1 <= len(batch) <= 12
        assert payload["fixed_decisions"] == batch
        assert payload["candidate_question"] == record["question"]
        inherited = payload["inherited_fixed_authority"]
        assert inherited["decision_ids"] == [rule["decision_id"] for rule in fixed]
        assert inherited["batch_rule_ids"] == [rule["decision_id"] for rule in batch]
        assert inherited["batch_rules_path"] == "fixed_decisions"
        assert payload[inherited["batch_rules_path"]] == batch
        assert inherited["application"] == "mandatory_preserved_base"
        assert inherited["snapshot_sha256"] == hashlib.sha256(json.dumps(fixed, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        assert payload["rule_bindings"] == BrainstormStageCoordinator._compile_fixed_rule_bindings(batch)
        assert payload["proposed_method_deltas"] == [{"option_id": option["id"], "actual_fields": {
            field: option[field] for field in ("label", "description", "tradeoff") if field in option}}
            for option in record["question"]["options"]]
        assert payload["validation_task"]["composition"] == "preserved_fixed_base_plus_actual_method_delta"
        assert payload["trusted_context"]["selected_topic"] == record["topic"]
        assert payload["trusted_context"]["node_goal"] == "比较说明段的局部组织方法"
        assert payload["validation_task"]["fact_order"] == MATRIX_FACTS
        assert payload["validation_task"]["fact_encoding"] == {"no": 0, "yes": 1, "unknown": 2}
        covered.extend(rule["decision_id"] for rule in batch)
        schema = service.model_calls[index]["json_schema"]["properties"]
        assert set(schema) == {rule["decision_id"] for rule in batch}
        for rule in batch:
            properties = schema[rule["decision_id"]]["properties"]
            assert set(properties) == {"fixed_evidence", "options"}
            assert properties["fixed_evidence"]["enum"] == [rule["answer"], "完整原说明与必要取舍"]
            cells = properties["options"]["properties"]
            assert set(cells) == {option["id"] for option in record["question"]["options"]}
            for cell in cells.values():
                facts = cell
                assert facts["type"] == "array"
                assert facts["minItems"] == facts["maxItems"] == 5
                assert facts["items"] == {"type": "integer", "enum": [0, 1, 2]}
        longest = {identifier: {"fixed_evidence": max(properties["properties"]["fixed_evidence"]["enum"], key=lambda value: len(value.encode("utf-8"))), "options": {option["id"]: [2] * 5
                for option in record["question"]["options"]}} for identifier, properties in schema.items()}
        assert service.model_calls[index]["num_predict"] >= 200 + len(
            json.dumps(longest, ensure_ascii=False, indent=2).encode("utf-8"))
        assert service.model_calls[index]["temperature"] == 0.0
    assert covered == [rule["decision_id"] for rule in fixed]
    assert all((1200 if option_count <= 2 else 1600) <= call["num_predict"] <= BrainstormStageCoordinator.MAX_SCOPE_OUTPUT_TOKENS
        for call in service.model_calls)
    for prompt in service.prompts:
        contract = json.loads(prompt)["comparison_contract"]
        assert contract["basis"] == "policy_proposal"
        assert contract["material_readiness"] == "not_evaluated_here"
        assert contract["all_facts_required"] is True


@pytest.mark.asyncio
async def test_inherited_base_is_program_bound_when_delta_does_not_repeat_old_rules():
    record, original, fixed = fixture()
    for index, option in enumerate(record["question"]["options"]):
        option.update(label="按对象组织" if index == 0 else "按时间组织",
            description="仅规定说明段内的局部组织方法")
    before = copy.deepcopy((record, fixed))
    responses = [matrix(fixed[offset:offset + 12], record["question"]) for offset in range(0, 24, 12)]
    service = await run_direct(fixed, record, responses)
    for prompt in service.prompts:
        payload = json.loads(prompt)
        assert payload["inherited_fixed_authority"]["decision_ids"] == [rule["decision_id"] for rule in fixed]
        assert all(delta["actual_fields"]["description"] == "仅规定说明段内的局部组织方法"
            for delta in payload["proposed_method_deltas"])
        assert all(rule["answer"] for rule in payload[payload["inherited_fixed_authority"]["batch_rules_path"]])
    assert len(service.model_calls) == 2 and (record, fixed) == before


def program_binding_fixture():
    _record, _original, fixed = fixture(7)
    current = fixed[4:7]
    inherited = {"snapshot_sha256": hashlib.sha256(json.dumps(fixed, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "decision_ids": [rule["decision_id"] for rule in fixed],
        "application": "mandatory_preserved_base",
        "batch_rule_ids": [rule["decision_id"] for rule in current], "batch_rules_path": "fixed_decisions"}
    return fixed, current, BrainstormStageCoordinator._compile_fixed_rule_bindings(current), inherited


@pytest.mark.parametrize("mutation", [
    lambda bindings, inherited: bindings["decision_5"].update(rule_sha256="0" * 64),
    lambda bindings, inherited: bindings["decision_5"].update(rule_sha256=bindings["decision_6"]["rule_sha256"]),
    lambda bindings, inherited: bindings["decision_5"].update(rule_sha256=bindings["decision_5"]["rule_sha256"].upper()),
    lambda bindings, inherited: bindings["decision_5"].update(rule_index=True),
    lambda bindings, inherited: bindings["decision_5"].update(rule_index=0.0),
    lambda bindings, inherited: bindings["decision_5"].update(rule_index=1),
    lambda bindings, inherited: bindings["decision_5"].update(extra="forged"),
    lambda bindings, inherited: bindings.pop("decision_5"),
    lambda bindings, inherited: bindings.update(forged=copy.deepcopy(bindings["decision_5"])),
    lambda bindings, inherited: inherited.update(snapshot_sha256="0" * 64),
    lambda bindings, inherited: inherited["decision_ids"].pop(),
    lambda bindings, inherited: inherited.update(batch_rules_path="untrusted_summary"),
    lambda bindings, inherited: inherited["batch_rule_ids"].reverse(),
    lambda bindings, inherited: inherited.update(extra="forged"),
])
def test_program_rule_bindings_reject_forged_stale_or_wrong_typed_identity(mutation):
    fixed, current, bindings, inherited = program_binding_fixture()
    BrainstormStageCoordinator._validate_fixed_rule_bindings(fixed=fixed, current=current,
        bindings=bindings, inherited=inherited)
    mutation(bindings, inherited)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        BrainstormStageCoordinator._validate_fixed_rule_bindings(fixed=fixed, current=current,
            bindings=bindings, inherited=inherited)


@pytest.mark.parametrize("field", ["answer", "selected_options", "user_inputs", "manually_edited"])
def test_binding_covers_the_entire_current_authority_not_only_answer(field):
    fixed, current, bindings, inherited = program_binding_fixture()
    changed = copy.deepcopy(current)
    changed[0][field] = {"complete_changed_authority": field}
    changed_bindings = BrainstormStageCoordinator._compile_fixed_rule_bindings(changed)
    assert changed_bindings["decision_5"]["rule_sha256"] != bindings["decision_5"]["rule_sha256"]
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        BrainstormStageCoordinator._validate_fixed_rule_bindings(fixed=fixed, current=changed,
            bindings=changed_bindings, inherited=inherited)


@pytest.mark.asyncio
async def test_authority_changing_while_checker_runs_cannot_use_a_stale_binding():
    record, _original, fixed = fixture(1)
    coordinator, service = setup([])
    async def mutate_authority(*args, **kwargs):
        response = matrix(fixed, record["question"])
        fixed[0]["answer"] = "运行期间已变更的当前真实选择"
        return json.dumps(response, ensure_ascii=False)
    coordinator._complete = mutate_authority
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={}, **MODEL)
    assert not service.model_calls


@pytest.mark.asyncio
async def test_long_complete_manual_authority_uses_bound_output_anchor_without_omitting_rules():
    record, _original, fixed = fixture()
    for rule in fixed:
        rule.update(answer=rule["answer"] + "完整真实背景与不能省略的现行政策。" * 170,
            manually_edited=True)
    responses = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    for response in responses:
        for identifier, item in response.items():
            rule = next(rule for rule in fixed if rule["decision_id"] == identifier)
            item["fixed_evidence"] = "policy:" + BrainstormStageCoordinator._compile_fixed_rule_bindings([rule])[identifier]["rule_sha256"]
    service = await run_direct(fixed, record, responses)
    assert len(service.model_calls) == 2
    for prompt, call in zip(service.prompts, service.model_calls):
        payload = json.loads(prompt)
        assert len(payload["fixed_decisions"]) == 12
        assert payload["rule_bindings"] == BrainstormStageCoordinator._compile_fixed_rule_bindings(payload["fixed_decisions"])
        assert all(len(rule["answer"]) > 1700 for rule in payload["fixed_decisions"])
        for index, rule in enumerate(payload["fixed_decisions"]):
            ref = payload["fixed_evidence_refs"][rule["decision_id"]]
            assert ref == {"rule_index": index, "evidence_ref": "policy:" + payload["rule_bindings"][rule["decision_id"]]["rule_sha256"]}
        assert 1200 <= call["num_predict"] <= 4000


@pytest.mark.asyncio
async def test_primary_compares_all_compatibility_facts_without_claiming_choice_novelty():
    record, _original, fixed = fixture()
    record["question"]["prompt"] = "请重新确认已选周期与全部既定来源"
    for option in record["question"]["options"]:
        option["description"] = "逐字复述已经确认的规则，不改变任何政策"
    responses = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    service = await run_direct(fixed, record, responses)
    assert len(service.model_calls) == 2
    payloads = [json.loads(prompt) for prompt in service.prompts]
    assert all("pairs" not in payload for payload in payloads)
    assert [rule["decision_id"] for payload in payloads for rule in payload["fixed_decisions"]] == [
        rule["decision_id"] for rule in fixed]
    assert all("这五项全0也不证明题目确实提出新的未决选择" in call["system_prompt"] for call in service.model_calls)


@pytest.mark.asyncio
async def test_later_actual_change_is_checked_after_earlier_unchanged_fixed_policy():
    record, _original, fixed = fixture()
    batches = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    batches[0]["decision_10"]["options"]["b"][3] = 1
    coordinator, service = setup([batches[0], pair_checks(batches[0], record["question"], fixed=fixed)])
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={}, **MODEL)
    assert error.value.decision_ids == ["decision_10"]
    assert [pair["fact_name"] for pair in json.loads(service.prompts[-1])["pairs"]] == [FACTS[3]]


@pytest.mark.asyncio
async def test_false_changes_clear_only_the_actual_requested_facts():
    record, _original, fixed = fixture(1)
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"] = [1, 0, 1, 0, 0]
    cleared = pair_checks(checked, record["question"], fixed=fixed, clear=True)
    service = await run_direct(fixed, record, [checked, cleared])
    payloads = [json.loads(prompt) for prompt in service.prompts]
    assert len(payloads) == 2
    assert [pair["fact_name"] for pair in payloads[1]["pairs"]] == [FACTS[0], FACTS[2]]


@pytest.mark.parametrize("retention_claim", ["", "保留全部原要求；"])
@pytest.mark.asyncio
async def test_delta_replacing_a_fixed_marker_is_rejected_despite_inherited_base(retention_claim):
    record, original, fixed = fixture()
    fixed[9].update(question="缺失项采用哪种视觉标记？", answer="使用灰色缺失符号并附注来源未提供")
    fixed[9]["selected_options"] = [{"id": "marker", "label": fixed[9]["answer"],
        "description": "每个缺失数据项都保留灰色符号与完整附注"}]
    record["question"]["options"][1].update(label="留白并在底部附注",
        description=retention_claim + "缺失项用留白呈现，在图表底部附注来源未提供")
    responses = [matrix(fixed[:12], record["question"])]
    responses[0]["decision_10"]["options"]["b"][3] = 1
    responses.append(pair_checks(responses[0], record["question"], fixed=fixed))
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await run_direct(fixed, record, responses)
    assert error.value.decision_ids == ["decision_10"]


@pytest.mark.asyncio
async def test_authority_hash_changes_only_with_real_base_not_candidate_retention_claim():
    record, original, fixed = fixture(1)
    first = await run_direct(fixed, record, [matrix(fixed, record["question"])])
    record["question"]["options"][1]["description"] = "候选声称保留全部，但仍须独立检验其真实新增方法"
    second = await run_direct(fixed, record, [matrix(fixed, record["question"])])
    fixed[0]["answer"] = "当前人工修订的真实规则"
    fixed[0]["manually_edited"] = True
    third = await run_direct(fixed, record, [matrix(fixed, record["question"])])
    hashes = [json.loads(service.prompts[0])["inherited_fixed_authority"]["snapshot_sha256"]
        for service in (first, second, third)]
    assert hashes[0] == hashes[1] and hashes[1] != hashes[2]


@pytest.mark.parametrize("fact_index", range(4))
@pytest.mark.asyncio
async def test_every_real_method_content_action_and_trigger_change_reports_actual_rule(fact_index):
    record, original, fixed = fixture()
    responses = [matrix(fixed[:12], record["question"])]
    responses[0]["decision_10"]["options"]["b"][fact_index] = 1
    responses.append(pair_checks(responses[0], record["question"], fixed=fixed))
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await run_direct(fixed, record, responses)
    assert error.value.decision_ids == ["decision_10"]


@pytest.mark.parametrize("fact_index", range(5))
@pytest.mark.asyncio
async def test_unknown_on_any_fact_fails_closed(fact_index):
    record, original, fixed = fixture(1)
    response = matrix(fixed, record["question"])
    response["decision_1"]["options"]["b"][fact_index] = 2
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await run_direct(fixed, record, [response])


@pytest.mark.parametrize("invalid", [True, False, 0.0, 1.0, 2.0, -1, 3, "0", "no", None, {}, []])
@pytest.mark.asyncio
async def test_compact_fact_encoding_requires_exact_finite_integers(invalid):
    record, _original, fixed = fixture(1)
    response = matrix(fixed, record["question"])
    response["decision_1"]["options"]["b"][4] = invalid
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_direct(fixed, record, [response])


@pytest.mark.parametrize("verdict", ["yes", "no", "unknown"])
@pytest.mark.asyncio
async def test_actual_repeat_fact_is_independently_grounded_after_complete_compatibility_coverage(verdict):
    record, _original, fixed = fixture()
    batches = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    batches[0]["decision_2"]["options"]["a"][4] = 1
    repetition = pair_checks(batches[0], record["question"], fixed=fixed, repeats=True)
    repetition["pair_0"]["verdict"] = verdict
    coordinator, service = setup(batches + [repetition])
    if verdict == "no":
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={}, **MODEL)
    else:
        with pytest.raises(BrainstormGenerationError,
                match="confirmed_decision_reopened" if verdict == "yes" else "insufficient_context") as error:
            await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
                context={}, **MODEL)
        if verdict == "yes":
            assert error.value.decision_ids == ["decision_2"]
    payloads = [json.loads(prompt) for prompt in service.prompts]
    assert [rule["decision_id"] for payload in payloads if "pairs" not in payload
        for rule in payload["fixed_decisions"]] == [rule["decision_id"] for rule in fixed]
    assert [pair["fact_name"] for pair in payloads[-1]["pairs"]] == [MATRIX_FACTS[4]]


@pytest.mark.parametrize("earlier_repeat", [1, 2])
@pytest.mark.asyncio
async def test_earlier_repeat_or_repeat_uncertainty_cannot_mask_later_true_compatibility_change(earlier_repeat):
    record, _original, fixed = fixture()
    batches = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    batches[0]["decision_2"]["options"]["a"][4] = earlier_repeat
    batches[1]["decision_24"]["options"]["b"][3] = 1
    coordinator, service = setup(batches + [pair_checks(batches[1], record["question"], fixed=fixed)])
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={}, **MODEL)
    assert error.value.decision_ids == ["decision_24"]
    assert [pair["fact_name"] for pair in json.loads(service.prompts[-1])["pairs"]] == [FACTS[3]]


@pytest.mark.asyncio
async def test_same_cell_four_false_changes_and_repeat_remain_five_distinct_recheck_facts():
    record, _original, fixed = fixture(1)
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"] = [1] * 5
    cleared = pair_checks(checked, record["question"], fixed=fixed, clear=True)
    repeat = pair_checks(checked, record["question"], fixed=fixed, clear=True, repeats=True)
    service = await run_direct(fixed, record, [checked, cleared, repeat])
    payloads = [json.loads(prompt) for prompt in service.prompts]
    assert [pair["fact_name"] for pair in payloads[1]["pairs"]] == FACTS
    assert [pair["fact_name"] for pair in payloads[2]["pairs"]] == [MATRIX_FACTS[4]]
    assert all(pair["actual_option"] == record["question"]["options"][1]
        for payload in payloads[1:] for pair in payload["pairs"])


@pytest.mark.parametrize("mutation", [
    lambda result: result["decision_1"].update(comparison_basis="policy_proposal"),
    lambda result: result["decision_1"].update(comparison_basis="material_readiness"),
    lambda result: result["decision_1"].pop("fixed_evidence"),
    lambda result: result["decision_1"]["options"].update(b={"policy_relation": "same", "facts": [0] * 5}),
    lambda result: result["decision_1"]["options"].update(b={"facts": [0] * 5}),
    lambda result: result.pop("decision_1"),
    lambda result: result.update(forged=copy.deepcopy(result["decision_1"])),
    lambda result: result["decision_1"].update(fixed_evidence="候选中编造的旧规则"),
    lambda result: result["decision_1"].update(rule_bindings={"rule_index": 0, "rule_sha256": "0" * 64}),
    lambda result: result["decision_1"]["options"].pop("b"),
    lambda result: result["decision_1"]["options"].update(forged={"facts": ["no"] * 4}),
    lambda result: result["decision_1"]["options"].update(b=[0] * 4),
    lambda result: result["decision_1"]["options"].update(b=[0] * 6),
    lambda result: result["decision_1"]["options"].update(b="0,0,0,0,0"),
    lambda result: result["decision_1"]["options"].update(b=0),
    lambda result: result["decision_1"]["options"].update(b=[0, 0, 3, 0, 0]),
    lambda result: result["decision_1"].update(extra="unchecked"),
])
@pytest.mark.asyncio
async def test_missing_forged_or_unbound_matrix_components_cannot_publish(mutation):
    record, original, fixed = fixture(1)
    response = matrix(fixed, record["question"])
    mutation(response)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_direct(fixed, record, [response])


@pytest.mark.asyncio
async def test_manual_answer_invalidates_obsolete_selected_option_evidence():
    record, original, fixed = fixture(1)
    fixed[0].update(answer="人工修订的新规则", manually_edited=True)
    response = matrix(fixed, record["question"])
    service = await run_direct(fixed, record, [response])
    payload = json.loads(service.prompts[0])
    assert payload["fixed_decisions"][0]["answer"] == "人工修订的新规则"
    assert payload["rule_bindings"] == BrainstormStageCoordinator._compile_fixed_rule_bindings(fixed)
    assert set(service.model_calls[0]["json_schema"]["properties"]["decision_1"]["properties"]) == {"fixed_evidence", "options"}
    response["decision_1"]["fixed_evidence"] = "完整原说明与必要取舍"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_direct(fixed, record, [response])


@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.asyncio
async def test_ordinary_negative_answer_is_still_a_complete_fixed_constraint(edited):
    record, original, fixed = fixture(1)
    fixed[0].update(answer="不要纳入个人手机号", manually_edited=edited)
    record["question"]["options"][1]["description"] = "在说明段列出完整个人手机号"
    response = matrix(fixed, record["question"])
    response["decision_1"]["options"]["b"][2] = 1
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await run_direct(fixed, record, [response, pair_checks(response, record["question"], fixed=fixed)])
    assert error.value.decision_ids == ["decision_1"]


@pytest.mark.parametrize("evidence_source", ["excluded_topic", "question"])
@pytest.mark.asyncio
async def test_explicit_exclusion_with_empty_answer_binds_actual_excluded_scope(evidence_source):
    record, original, fixed = fixture(1)
    fixed[0].update(answer="", answer_source="user_excluded", excluded_topic="个人资料读取",
        question="是否需要读取个人资料来安排说明？")
    response = matrix(fixed, record["question"])
    assert fixed[0][evidence_source]
    service = await run_direct(fixed, record, [response])
    payload = json.loads(service.prompts[0])
    assert payload["fixed_decisions"] == fixed
    assert payload["rule_bindings"] == BrainstormStageCoordinator._compile_fixed_rule_bindings(fixed)
    assert payload["fixed_decisions"][0]["answer_source"] == "user_excluded"
    assert "不是确认题内假设" in service.model_calls[0]["system_prompt"]


@pytest.mark.parametrize("later_change", [False, True])
@pytest.mark.asyncio
async def test_clearing_d4_false_prerequisite_does_not_skip_d10_or_remaining_authority(later_change):
    record, original, fixed = fixture()
    fixed[3].update(question="数据缺失或指标异常时应如何处理？",
        answer="标注数据缺失并暂停该指标趋势分析")
    fixed[9].update(question="缺失项使用哪种符号？", answer="灰色缺失符号并附注来源未提供")
    record["question"]["prompt"] = "在既定缺失处理规则内，如何组织图下注释说明？"
    record["question"]["options"][0].update(label="按对象分块说明",
        description="在图下注释区按对象分块解释灰色缺失符号和来源未提供标注")
    record["question"]["options"][1].update(label="按来源分块说明",
        description="在图下注释区按来源分块解释灰色缺失符号和来源未提供标注")
    batches = [matrix(fixed[offset:offset + 12], record["question"]) for offset in range(0, 24, 12)]
    batches[0]["decision_4"]["options"]["a"][0] = 1
    if later_change:
        record["question"]["options"][1].update(label="留白附注", description="缺失值改用留白，底部附注来源未提供")
        batches[0]["decision_10"]["options"]["b"][3] = 1
    clear = pair_checks(batches[0], record["question"], fixed=fixed, clear=True)
    if later_change:
        clear["pair_1"]["verdict"] = "yes"
        coordinator, service = setup([batches[0], clear])
        with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
            await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
                context={"selected_topic": record["topic"], "node_goal": "比较图下注释的分块方法",
                    "ancestor_conditions": record["ancestor_conditions"]}, **MODEL)
        assert error.value.decision_ids == ["decision_10"]
    else:
        service = await run_direct(fixed, record, [batches[0], clear] + batches[1:])
        assert len(service.model_calls) == 3
        matrices = [json.loads(prompt) for prompt in service.prompts if "pairs" not in json.loads(prompt)]
        assert [rule["decision_id"] for payload in matrices for rule in payload["fixed_decisions"]] == [
            rule["decision_id"] for rule in fixed]
        assert all(set(payload["candidate_question"]["options"][index]) == set(record["question"]["options"][index])
            for payload in matrices for index in range(2))
    payload = json.loads(service.prompts[1])
    assert len(payload["pairs"]) == (2 if later_change else 1)
    assert payload["pairs"][0]["decision_id"] == "decision_4"
    assert payload["pairs"][0]["option_id"] == "a"
    assert payload["pairs"][0]["fixed_decision"] == fixed[3]
    assert payload["pairs"][0]["actual_option"] == record["question"]["options"][0]
    inherited = payload["inherited_fixed_authority"]
    assert inherited["decision_ids"] == [rule["decision_id"] for rule in fixed]
    assert inherited["batch_rule_ids"] == (["decision_4", "decision_10"] if later_change else ["decision_4"])
    assert payload[inherited["batch_rules_path"]] == ([fixed[3], fixed[9]] if later_change else [fixed[3]])
    schema = service.model_calls[1]["json_schema"]["properties"]["pair_0"]["properties"]
    assert schema["candidate_evidence"]["enum"] == ["\n".join(record["question"]["options"][0][field]
        for field in ("label", "description", "tradeoff") if record["question"]["options"][0].get(field))]
    assert record["question"]["prompt"] not in schema["candidate_evidence"]["enum"]
    assert payload["pairs"][0]["fact_name"] == FACTS[0]


@pytest.mark.parametrize("fact_index,old_rule,delta", [
    (0, "资料缺失时立即暂停分析", "资料缺失仅在三次重新采集仍失败后才暂停分析"),
    (1, "资料缺失时暂停分析", "资料缺失时使用估算值继续分析"),
    (2, "必须保留利用率与成本两项数据", "仅保留成本数据，不纳入利用率"),
    (3, "缺失值使用灰色符号", "缺失值改用留白"),
])
@pytest.mark.asyncio
async def test_grounded_recheck_keeps_each_real_change_blocking(fact_index, old_rule, delta):
    record, original, fixed = fixture(1)
    fixed[0].update(answer=old_rule, manually_edited=True)
    record["question"]["options"][1]["description"] = delta
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"][fact_index] = 1
    confirmation = pair_checks(checked, record["question"], fixed=fixed)
    coordinator, service = setup([checked, confirmation])
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={"node_goal": "比较对象内方法", "ancestor_conditions": []}, **MODEL)
    assert error.value.decision_ids == ["decision_1"]
    assert len(service.model_calls) == 2
    assert json.loads(service.prompts[1])["pairs"][0]["actual_option"]["description"] == delta
    evidence = service.model_calls[1]["json_schema"]["properties"]["pair_0"]["properties"]["fixed_evidence"]["enum"]
    assert evidence == [BrainstormStageCoordinator._fixed_policy_evidence(fixed[0])]
    policy = json.loads(evidence[0])
    assert policy["object_context"]["question"] == fixed[0]["question"]
    assert policy["effective_user_policy"]["current_answer"] == old_rule
    assert policy["effective_user_policy"]["effective_selected_explanations"] == []


@pytest.mark.parametrize("fact_index", range(4))
@pytest.mark.asyncio
async def test_unknown_in_independent_negative_recheck_still_blocks(fact_index):
    record, original, fixed = fixture(1)
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"][fact_index] = 1
    confirmation = pair_checks(checked, record["question"], fixed=fixed, clear=True)
    confirmation["pair_0"]["verdict"] = "unknown"
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await run_direct(fixed, record, [checked, confirmation])


@pytest.mark.parametrize("mutation", [
    lambda checked: checked.pop("pair_0"),
    lambda checked: checked["pair_0"].update(comparison_basis="policy_proposal"),
    lambda checked: checked["pair_0"].update(comparison_basis="material_readiness"),
    lambda checked: checked["pair_0"].pop("fact_name"),
    lambda checked: checked["pair_0"].update(policy_relation="invented"),
    lambda checked: checked["pair_0"].update(policy_relation=False),
    lambda checked: checked.update(forged=copy.deepcopy(checked["pair_0"])),
    lambda checked: checked["pair_0"].update(fixed_evidence="候选制造的旧规则"),
    lambda checked: checked["pair_0"].update(candidate_evidence="未出现的候选行为"),
    lambda checked: checked["pair_0"].pop("candidate_evidence"),
    lambda checked: checked["pair_0"].update(fact_name=FACTS[1]),
    lambda checked: checked["pair_0"].update(verdict=False),
    lambda checked: checked["pair_0"].update(verdict="no,no,no,no"),
    lambda checked: checked["pair_0"].update(facts=["no", "no", "invented", "no"]),
    lambda checked: checked["pair_0"].update(extra="未绑定字段"),
])
@pytest.mark.asyncio
async def test_unbound_missing_or_forged_negative_recheck_cannot_clear_pair(mutation):
    record, original, fixed = fixture(1)
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"][0] = 1
    confirmation = pair_checks(checked, record["question"], fixed=fixed, clear=True)
    mutation(confirmation)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_direct(fixed, record, [checked, confirmation])


@pytest.mark.parametrize("later_invalid", ["unknown", "missing", "forged"])
@pytest.mark.asyncio
async def test_cleared_negative_pair_cannot_bypass_later_invalid_matrix(later_invalid):
    record, original, fixed = fixture()
    first = matrix(fixed[:12], record["question"])
    first["decision_4"]["options"]["a"][0] = 1
    second = matrix(fixed[12:24], record["question"])
    if later_invalid == "unknown":
        second["decision_16"]["options"]["b"][3] = 2
    elif later_invalid == "missing":
        second.pop("decision_16")
    else:
        second["decision_16"]["fixed_evidence"] = "伪造规则"
    with pytest.raises(BrainstormGenerationError,
            match="insufficient_context" if later_invalid == "unknown" else "checker_invalid"):
        await run_direct(fixed, record, [first, pair_checks(first, record["question"], fixed=fixed, clear=True), second])


@pytest.mark.parametrize("large_evidence", [False, True])
@pytest.mark.asyncio
async def test_all_disputed_pairs_have_bounded_complete_evidence_batches(large_evidence):
    record, original, fixed = fixture(24, 5)
    if large_evidence:
        for rule in fixed:
            rule["answer"] = rule["answer"] + "完整真实固定对象与实际作用范围。" * 8
        for option in record["question"]["options"]:
            option["description"] += "新增局部方法与既定内容都逐字完整保留。" * 8
    class MatrixPairService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            if "pairs" in payload:
                response = {key: {"fixed_evidence": schema["properties"]["fixed_evidence"]["enum"][0],
                    "candidate_evidence": schema["properties"]["candidate_evidence"]["enum"][-1],
                    "fact_name": schema["properties"]["fact_name"]["enum"][0],
                    "verdict": "no"} for key, schema in kwargs["json_schema"]["properties"].items()}
            else:
                response = matrix(payload["fixed_decisions"], payload["candidate_question"])
                if payload["fixed_decisions"][0]["decision_id"] == "decision_1":
                    for entry in response.values():
                        for cell in entry["options"].values():
                            cell[3] = 1
            yield json.dumps(response, ensure_ascii=False)
    service = MatrixPairService([])
    coordinator = BrainstormStageCoordinator(service)
    await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
        context={"node_goal": "比较对象内方法", "ancestor_conditions": []}, **MODEL)
    calls = [(json.loads(prompt), call) for prompt, call in zip(service.prompts, service.model_calls)]
    rechecks = [(payload, call) for payload, call in calls if "pairs" in payload]
    actual_pairs = [(pair["decision_id"], pair["option_id"]) for payload, call in rechecks for pair in payload["pairs"]]
    first_matrix = next(payload for payload, _call in calls if "pairs" not in payload)
    assert actual_pairs == [(rule["decision_id"], option["id"]) for rule in first_matrix["fixed_decisions"]
        for option in record["question"]["options"]]
    assert all(1 <= len(payload["pairs"]) <= 6 for payload, call in rechecks)
    assert all(1200 <= call["num_predict"] <= coordinator.MAX_SCOPE_OUTPUT_TOKENS for payload, call in rechecks)
    for payload, call in rechecks:
        assert set(call["json_schema"]["properties"]) == {pair["pair_id"] for pair in payload["pairs"]}
        assert call["num_predict"] >= 200 + len(json.dumps({
            pair["pair_id"]: {"fixed_evidence": pair["fixed_decision"]["answer"],
                "candidate_evidence": pair["actual_option"]["description"],
                "fact_name": pair["fact_name"], "verdict": "unknown"}
            for pair in payload["pairs"]}, ensure_ascii=False, indent=2).encode("utf-8"))
    matrices = [payload for payload, call in calls if "pairs" not in payload]
    assert [rule["decision_id"] for payload in matrices for rule in payload["fixed_decisions"]] == [rule["decision_id"] for rule in fixed]


@pytest.mark.parametrize("fact_index", range(4))
@pytest.mark.asyncio
async def test_each_primary_fact_change_requires_its_exact_independent_verdict(fact_index):
    record, _original, fixed = fixture(1)
    checked = matrix(fixed, record["question"])
    checked["decision_1"]["options"]["b"][fact_index] = 1
    pair = pair_checks(checked, record["question"], fixed=fixed)
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await run_direct(fixed, record, [checked, pair])
    assert error.value.decision_ids == ["decision_1"]
    assert pair["pair_0"]["fact_name"] == FACTS[fact_index]


@pytest.mark.parametrize("replaces_detail", [False, True])
@pytest.mark.asyncio
async def test_carrier_detail_false_change_does_not_hide_a_confirmed_detail_replacement(replaces_detail):
    record, _original, fixed = fixture()
    fixed[2].update(question="核心指标使用哪种呈现载体？", answer="趋势折线图", manually_edited=True)
    fixed[9].update(question="缺失项使用哪种视觉标记？", answer="灰色符号并附注来源未提供", manually_edited=True)
    record["question"]["prompt"] = "在既定载体中，缺失说明如何组织？"
    record["question"]["options"][0].update(label="符号说明按对象分组",
        description="仍使用趋势折线图与灰色缺失符号，来源说明按对象分组")
    record["question"]["options"][1].update(label="按来源分组",
        description="仍使用趋势折线图与灰色缺失符号，来源说明按来源分组")
    if replaces_detail:
        record["question"]["options"][1].update(label="留白附注",
            description="仍使用趋势折线图，缺失项改为留白并在底部附注来源未提供")
    batches = [matrix(fixed[offset:offset + 12], record["question"]) for offset in range(0, 24, 12)]
    batches[0]["decision_3"]["options"]["a"][3] = 1
    if replaces_detail:
        batches[0]["decision_10"]["options"]["b"][3] = 1
    clear = pair_checks(batches[0], record["question"], fixed=fixed, clear=True)
    if replaces_detail:
        clear["pair_1"]["verdict"] = "yes"
        with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
            await run_direct(fixed, record, [batches[0], clear])
        assert error.value.decision_ids == ["decision_10"]
    else:
        service = await run_direct(fixed, record, [batches[0], clear] + batches[1:])
        payloads = [json.loads(prompt) for prompt in service.prompts]
        covered = [rule["decision_id"] for payload in payloads if "pairs" not in payload
            for rule in payload["fixed_decisions"]]
        assert covered == [rule["decision_id"] for rule in fixed]
        assert all(payload["comparison_contract"]["material_readiness"] == "not_evaluated_here" for payload in payloads)


@pytest.mark.parametrize("forged_at", [None, "primary", "pair"])
@pytest.mark.asyncio
async def test_long_manual_policy_uses_bound_sha_evidence_and_true_change_remains_blocking(forged_at):
    record, _original, fixed = fixture(1)
    fixed[0].update(answer="缺失值必须保留灰色符号；" + "完整政策背景说明。" * 170,
        manually_edited=True)
    record["question"]["options"][1].update(label="留白", description="缺失项改用留白")
    before = copy.deepcopy((fixed, record))
    expected = "policy:" + hashlib.sha256(json.dumps(fixed[0], ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    class LongPolicyService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            schema = kwargs["json_schema"]["properties"]
            if "pairs" in payload:
                assert payload["pairs"][0]["fixed_decision"] == fixed[0]
                assert payload["pairs"][0]["actual_option"] == record["question"]["options"][1]
                assert schema["pair_0"]["properties"]["fixed_evidence"]["enum"] == [expected]
                response = {"pair_0": {"fixed_evidence": expected,
                    "candidate_evidence": "\n".join(record["question"]["options"][1][field]
                        for field in ("label", "description", "tradeoff") if record["question"]["options"][1].get(field)),
                    "fact_name": FACTS[3], "verdict": "yes"}}
                if forged_at == "pair":
                    response["pair_0"]["fixed_evidence"] = "policy:" + "0" * 64
            else:
                assert payload["fixed_decisions"] == fixed
                assert payload["rule_bindings"]["decision_1"]["rule_sha256"] == expected.removeprefix("policy:")
                response = matrix(fixed, record["question"])
                response["decision_1"]["fixed_evidence"] = expected
                response["decision_1"]["options"]["b"][3] = 1
                if forged_at == "primary":
                    response["decision_1"]["fixed_evidence"] = "policy:" + "0" * 64
            if "pairs" in payload:
                assert list(payload["fixed_evidence_refs"].values())[0]["evidence_ref"] == expected
            yield json.dumps(response, ensure_ascii=False)
    service = LongPolicyService([])
    coordinator = BrainstormStageCoordinator(service)
    code = "checker_invalid" if forged_at else "confirmed_decision_conflict"
    with pytest.raises(BrainstormGenerationError, match=code) as error:
        await coordinator._check_fixed_decision_matrix(question=record["question"], fixed=fixed,
            context={"node_goal": "比较说明方法", "ancestor_conditions": []}, **MODEL)
    if forged_at is None:
        assert error.value.decision_ids == ["decision_1"]
    assert all(1200 <= call["num_predict"] <= coordinator.MAX_SCOPE_OUTPUT_TOKENS
        and call["temperature"] == 0.0 for call in service.model_calls)
    assert (fixed, record) == before


@pytest.mark.asyncio
async def test_negative_pair_recheck_uses_shared_step_deadline_without_extension(monkeypatch):
    record, original, fixed = fixture(1)
    checked = scope_checks(record)
    first_matrix = matrix(fixed, record["question"])
    first_matrix["decision_1"]["options"]["b"][0] = 1
    closed = asyncio.Event()
    class SlowPairService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            if len(self.model_calls) == 1:
                yield json.dumps(checked, ensure_ascii=False)
            elif len(self.model_calls) == 2:
                yield json.dumps(first_matrix, ensure_ascii=False)
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = SlowPairService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.08)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert closed.is_set() and len(service.model_calls) == 3

def proof(record):
    return {"unresolved_axis": record["question"]["prompt"], "relation": "same_unresolved_axis",
        "decision_ids": [], "options": {option["id"]: {"evidence": option["label"],
            "role": "new_method", "fixed_id": "none", "action_span": "\n".join(option[field] for field in ("label", "description", "tradeoff")
                if isinstance(option.get(field), str) and option[field].strip())}
            for option in record["question"]["options"]}}


def scope_cell(candidate_evidence, trusted_goal, *, parent=False, future=False, claim_evidence=None,
               writing_organization=False, selected_skills=None, parent_evidence_ref=None):
    cell = {"candidate_evidence": candidate_evidence, "trusted_goal_evidence": trusted_goal,
        "scope": "within", "parent": "preserves" if parent else "no_parent",
        "cleared": "no_cleared", "exclusion": "no_exclusions",
        "claim_evidence": candidate_evidence if claim_evidence is None else claim_evidence,
        "claims_current_candidate_selected": "no", "claims_hypothetical_path_confirmed": "no",
        "asserts_unconfirmed_realized_fact": "no",
        "asserts_unconfirmed_user_choice": "no",
        "future_fixed": "pass" if future else "not_applicable"}
    if parent_evidence_ref is not None:
        cell["parent_evidence_ref"] = parent_evidence_ref
    if writing_organization:
        cell["proposes_nonwriting_operation"] = "no"
    if selected_skills:
        cell["selected_skill_policy_ref"] = hashlib.sha256(json.dumps(selected_skills,
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        cell["violates_selected_skill_policy"] = "no"
    return cell


def scope_parent_ref(record, identifier, kind, *, option_id=None, text=None):
    # Only provide the source identity required by the actual parent schema.
    # The caller's existing parent verdict remains unchanged and independent.
    candidate = BrainstormStageCoordinator._consistency_candidate({"review_questions": [record]})
    selected = candidate["review_questions"][0]
    context = {"question": selected["question"], "ancestor_conditions": selected["ancestor_conditions"]}
    unit = {"id": identifier, "kind": kind, "context_key": "q0"}
    if kind == "option":
        unit["option_id"] = option_id
    elif kind == "next_goal":
        unit.update(branch_option_id=option_id, text=text)
    binding = BrainstormStageCoordinator._parent_scope_binding(unit, context)
    return None if binding is None else BrainstormStageCoordinator._scope_snapshot_sha(binding)


def scope_checks(record, goal=None, *, writing_organization=False, selected_skills=None):
    goal = goal or record["topic"]["goal"]
    annotations = record.get("candidate_copy", record["question"])
    question_claim = "\n".join([record["question"]["prompt"]] + [annotations[field]
        for field in ("why_now", "single_choice_reason", "answer_template")
        if isinstance(annotations.get(field), str) and annotations[field].strip()])
    checks = {identifier: scope_cell(record["question"]["prompt"], goal,
        claim_evidence=question_claim if identifier == "q0_prompt" else record["question"]["prompt"],
        parent=bool(record.get("ancestor_conditions")), writing_organization=writing_organization,
        selected_skills=selected_skills, parent_evidence_ref=scope_parent_ref(record, identifier,
            "question" if identifier == "q0_prompt" else "choice_scope"))
        for identifier in ("q0_prompt", "q0_space")}
    checks.update({"q0_o{}".format(index): scope_cell(option["label"], goal,
        parent=bool(record.get("ancestor_conditions")), claim_evidence="\n".join(option[field]
            for field in ("label", "description", "tradeoff")
            if isinstance(option.get(field), str) and option[field].strip()),
        writing_organization=writing_organization, selected_skills=selected_skills,
        parent_evidence_ref=scope_parent_ref(record, "q0_o{}".format(index), "option", option_id=option["id"]))
        for index, option in enumerate(record["question"]["options"])})
    return {"checks": checks}


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.asyncio
async def test_real_new_axis_uses_full_matrix_even_when_primary_binds_unrelated_single_rule(changed):
    record, original, fixed = fixture()
    checked = scope_checks(record)
    responses = [matrix(fixed[offset:offset + 12], record["question"])
        for offset in range(0, 24, 12)]
    if changed:
        record["question"]["options"][1]["description"] = "改用留白替代用户已定灰色缺失符号"
        responses[0]["decision_10"]["options"]["b"][3] = 1
        responses.insert(1, pair_checks(responses[0], record["question"], fixed=fixed))
        checked = scope_checks(record)
    coordinator, service = setup([checked] + responses, check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    async def forbidden_subset(**kwargs):
        pytest.fail("Valid new-axis certificate must use the full matrix instead of selected-ID subset")
    coordinator._recheck_bound_fixed_rules = forbidden_subset
    coordinator._recheck_negative_decision_relation = forbidden_subset
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original)
    expected = {"review": "reject", "codes": ["confirmed_decision_conflict"],
        "decision_ids": ["decision_10"]} if changed else {"review": "pass", "codes": [], "decision_ids": []}
    assert result == expected
    assert len(service.model_calls) == 3
    schema = service.model_calls[0]["json_schema"]["properties"]
    assert set(schema) == {"checks"}
    fields = schema["checks"]["properties"]["q0_o1"]["properties"]
    assert fields["future_fixed"]["enum"] == ["not_applicable"]
    assert fields["exclusion"]["enum"] == ["no_exclusions"]
    assert "整组选项新取舍" in service.model_calls[0]["system_prompt"]


@pytest.mark.parametrize("code", ["topic_scope", "parent_condition_conflict",
    "cleared_constraint_conflict", "candidate_as_confirmed", "exclusion_conflict:decision_1"])
@pytest.mark.asyncio
async def test_primary_scope_fact_and_parent_violations_remain_blocking(code):
    record, original, fixed = fixture(1)
    if code == "parent_condition_conflict":
        record["ancestor_conditions"] = [{"parent_question_id": "actual-parent", "parent_option_id": "a",
            "parent_prompt": "沿用哪个父方法？", "option_label": "先说明全部结果",
            "option_description": "沿用原结果说明方法"}]
        record["question"].update(parent_question_id="actual-parent", parent_option_id="a")
    if code.startswith("exclusion_conflict"):
        original[0]["answer_source"] = "user_excluded"
    checked = scope_checks(record)
    field, value = {"topic_scope": ("scope", "outside"),
        "parent_condition_conflict": ("parent", "violates"),
        "cleared_constraint_conflict": ("cleared", "restores"),
        "candidate_as_confirmed": ("claims_current_candidate_selected", "yes"),
        "exclusion_conflict:decision_1": ("exclusion", "violates:decision_1")}[code]
    if code == "cleared_constraint_conflict":
        for cell in checked["checks"].values():
            cell["cleared"] = "respects"
    if code.startswith("exclusion_conflict"):
        for cell in checked["checks"].values():
            cell["exclusion"] = "respects"
    checked["checks"]["q0_o1"][field] = value
    coordinator, service = setup([checked], check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    async def forbidden_matrix(**kwargs):
        pytest.fail("Scope/fact violations must stop before compatibility comparison")
    coordinator._check_fixed_decision_matrix = forbidden_matrix
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original,
        brief_edits={"fixed-1": ""} if code == "cleared_constraint_conflict" else {})
    expected_code, _, identifier = code.partition(":")
    assert result == {"review": "reject", "codes": [expected_code],
        "decision_ids": [identifier] if identifier else []}
    assert len(service.model_calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda checked: checked.update(decision_spaces={}),
    lambda checked: checked["checks"].update(q0_o1="confirmed_decision_conflict:decision_1"),
    lambda checked: checked["checks"].update(q0_o1="confirmed_decision_reopened:decision_1"),
])
@pytest.mark.asyncio
async def test_primary_cannot_emit_a_second_compatibility_or_relation_verdict(mutation):
    record, original, fixed = fixture(1)
    checked = scope_checks(record)
    mutation(checked)
    coordinator, service = setup([checked], check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert len(service.model_calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda cell: cell.update(candidate_evidence="另一个选项的虚构证据"),
    lambda cell: cell.update(trusted_goal_evidence="从候选编造的主题目标"),
    lambda cell: cell.pop("candidate_evidence"),
    lambda cell: cell.update(extra="未绑定说明"),
    lambda cell: cell.update(parent="unknown"),
    lambda cell: cell.update(cleared="unknown"),
    lambda cell: cell.update(exclusion="unknown"),
])
@pytest.mark.asyncio
async def test_scope_proof_requires_actual_unit_goal_and_explicit_absent_conditions(mutation):
    record, original, fixed = fixture(1)
    checked = scope_checks(record)
    mutation(checked["checks"]["q0_o1"])
    coordinator, service = setup([checked], check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)


@pytest.mark.parametrize("field", ["scope", "claims_current_candidate_selected"])
@pytest.mark.asyncio
async def test_true_scope_or_fact_uncertainty_remains_blocking(field):
    record, original, fixed = fixture(1)
    checked = scope_checks(record)
    checked["checks"]["q0_o1"][field] = "unknown"
    coordinator, service = setup([checked], check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_review_binds_topic_goal_without_requiring_missing_node_goal():
    record, original, fixed = fixture(1)
    coordinator, service = setup([scope_checks(record), matrix(fixed, record["question"])], check_consistency=True)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original)
    context = json.loads(service.prompts[0])["question_contexts"]["q0"]
    assert "node_goal" not in context
    assert context["effective_goal_source"] == "selected_topic.goal"
    assert context["effective_goal"] == record["topic"]["goal"]
    schemas = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]
    for identifier in review_units():
        fields = schemas[identifier]["properties"]
        assert fields["trusted_goal_evidence"]["enum"] == [record["topic"]["goal"]]
        assert fields["parent"]["enum"] == ["no_parent"]
        assert fields["cleared"]["enum"] == ["no_cleared"]
        assert fields["exclusion"]["enum"] == ["no_exclusions"]


@pytest.mark.asyncio
async def test_eight_large_scope_evidence_units_split_with_complete_bounded_output():
    record, original, fixed = fixture(1)
    record["topic"]["goal"] = "组织" * 40
    record["question"]["prompt"] = "说明" * 30
    for option in record["question"]["options"]:
        option["label"] = option["id"] + "组织" * 11
        option["description"] = "完整说明" * 20
    second = copy.deepcopy(record)
    second["question"]["id"] += "-second"
    class FiniteSchemaService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            self.prompts.append(kwargs["user_prompt"])
            properties = kwargs["json_schema"]["properties"]
            if "checks" in properties:
                checked = {identifier: {field: "no" if value["enum"] == ["yes", "no", "unknown"]
                    else value["enum"][0] for field, value in schema["properties"].items()}
                    for identifier, schema in properties["checks"]["properties"].items()}
                yield json.dumps({"checks": checked}, ensure_ascii=False, indent=2)
            else:
                payload = json.loads(kwargs["user_prompt"])
                yield json.dumps(matrix(payload["fixed_decisions"], payload["candidate_question"]), ensure_ascii=False)
    service = FiniteSchemaService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record), proof(second)]
    coordinator._check_new_decision_space = relationship
    await prepare(coordinator, kind="review", review_questions=[record, second],
        plan={"topics": [record["topic"]]}, decisions=original)
    scope_calls = [call for call in service.model_calls if "checks" in call["json_schema"]["properties"]]
    assert len(scope_calls) >= 2
    covered = []
    for call in scope_calls:
        schemas = call["json_schema"]["properties"]["checks"]["properties"]
        covered.extend(schemas)
        assert len(schemas) <= BrainstormStageCoordinator.MAX_CONSISTENCY_UNITS
        assert 1200 <= call["num_predict"] <= BrainstormStageCoordinator.MAX_SCOPE_OUTPUT_TOKENS
        assert call["num_predict"] == coordinator._bound_scope_output_tokens(schemas)
        for schema in schemas.values():
            assert all(len(value) <= 80 for value in schema["properties"]["trusted_goal_evidence"]["enum"])
    assert covered == review_units(2)
    matrix_calls = [json.loads(call["user_prompt"]) for call in service.model_calls
        if "candidate_question" in json.loads(call["user_prompt"])]
    assert len(service.model_calls) == len(scope_calls) + len(matrix_calls)
    assert [rule["decision_id"] for payload in matrix_calls for rule in payload["fixed_decisions"]] == [
        rule["decision_id"] for rule in fixed] * 2


@pytest.mark.parametrize("code", ["confirmed_decision_reopened", "confirmed_decision_conflict"])
@pytest.mark.asyncio
async def test_free_future_goal_has_independent_fixed_compatibility_without_rechecking_current_choice(code):
    record, original, fixed = fixture(1)
    result = {"question": record["question"], "next_goals": [
        {"option_id": "a", "goals": ["重新选择原表现形式"]},
        {"option_id": "b", "goals": ["安排后续段内解释"]}]}
    checked = scope_checks(record)
    checked["checks"].update({"g{}_0".format(index): scope_cell(group["goals"][0], record["topic"]["goal"],
        parent=True, future=True, parent_evidence_ref=scope_parent_ref(record, "g{}_0".format(index),
            "next_goal", option_id=group["option_id"], text=group["goals"][0]))
        for index, group in enumerate(result["next_goals"])})
    checked["checks"]["g0_0"]["future_fixed"] = code + ":decision_1"
    coordinator, service = setup([checked], check_consistency=True)
    async def bounded_scope(**kwargs):
        service.prompts.append(kwargs["user_prompt"])
        service.model_calls.append(kwargs)
        schemas = kwargs["json_schema"]["properties"]["checks"]["properties"]
        yield json.dumps({"checks": {identifier: checked["checks"][identifier]
            for identifier in schemas}}, ensure_ascii=False)
    service._stream_direct_completion = bounded_scope
    async def relationship(**kwargs):
        return [proof({"question": kwargs["candidate"]["question"]})]
    coordinator._check_new_decision_space = relationship
    async def forbidden_matrix(**kwargs):
        pytest.fail("Invalid freely planned future goal must be rejected before current-choice matrix")
    coordinator._check_fixed_decision_matrix = forbidden_matrix
    with pytest.raises(BrainstormGenerationError, match=code) as error:
        await coordinator._check_stage_consistency(stage="detail", kind="node", topic=record["topic"],
            task={"fixed_decisions": fixed, "ancestor_conditions": [], "goal": record["topic"]["goal"],
                "cleared_constraints": [], "user_constraints": {}}, result=result,
            other_topics=[], **MODEL)
    assert error.value.decision_ids == ["decision_1"] and len(service.model_calls) == 1
    checks = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]
    assert code + ":decision_1" in checks["g0_0"]["properties"]["future_fixed"]["enum"]
    for identifier in review_units():
        assert checks[identifier]["properties"]["future_fixed"]["enum"] == ["not_applicable"]
    payload = json.loads(service.prompts[0])
    goal = next(unit for unit in payload["validation_task"]["units"] if unit["id"] == "g0_0")
    assert goal["fixed_compatibility_required"] is True and goal["branch_option_id"] == "a"


@pytest.mark.parametrize("with_skill", [False, True])
@pytest.mark.asyncio
async def test_default_writing_future_goals_are_program_bound_and_keep_scope_only_protocol(with_skill):
    original = decisions(24)
    fixed = BrainstormStageCoordinator._stage_authority(original, {})[0]
    next_goal = BrainstormStageCoordinator.WRITING_AXES[0]["levels"][1]
    selected_skills = ([{"id": "organize", "name": "章节组织", "content": "保留原章节对象与分类，仅比较段内组织。",
        "metadata": {"version": 3, "source": "完整已选Skill"}}] if with_skill else [])
    chapter = {"id": "chapter1", "title": "人物与事件说明",
        "goal": "交代本章人物和事件，不承担其余章节主任务"}
    chapter_ref = hashlib.sha256(json.dumps(chapter, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    captured = {}
    class CompiledWritingService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            assert "generation_task" not in payload
            properties = kwargs["json_schema"]["properties"]
            if "checks" in properties:
                schemas = properties["checks"]["properties"]
                response = {"checks": {identifier: captured["checks"][identifier] for identifier in schemas}}
            else:
                assert payload["candidate_question"] == captured["candidate"]["question"]
                response = matrix(payload["fixed_decisions"], payload["candidate_question"])
            yield json.dumps(response, ensure_ascii=False)
    coordinator, _unused = setup([], check_consistency=True)
    service = CompiledWritingService([])
    coordinator.creation_service = service
    async def relationship(**kwargs):
        captured["candidate"] = copy.deepcopy(kwargs["candidate"])
        captured["task"] = copy.deepcopy(kwargs["task"])
        record = {"question": kwargs["candidate"]["question"], "topic": kwargs["topic"],
            "candidate_copy": kwargs["candidate"].get("candidate_copy", {}),
            "ancestor_conditions": kwargs["task"]["ancestor_conditions"]}
        checked = scope_checks(record, kwargs["task"]["node_goal"],
            writing_organization=True, selected_skills=selected_skills)
        for cell in checked["checks"].values():
            cell["chapter_evidence_ref"] = chapter_ref
        captured["checks"] = checked["checks"]
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    result = await prepare(coordinator, stage="writing", decisions=original, selected_skills=selected_skills)
    task = captured["task"]
    assert task["program_bound_next_goals"] is True and task["program_bound_editorial"] is True
    coordinator._verify_editorial_compilation(result=result, task=task, topic=task["selected_topic"])
    assert task["editorial_compilation"]["inputs"]["authority"]["fixed_decisions"] == fixed
    assert [option["label"] for option in result["question"]["options"]] == ["先概括再解释", "先解释再归纳"]
    primary = json.loads(service.prompts[0])
    assert primary["authority"]["user_constraints"]["selected_skills"] == selected_skills
    schemas = service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]
    for identifier, schema in schemas.items():
        assert set(captured["checks"][identifier]) == set(schema["properties"])
        assert schema["properties"]["proposes_nonwriting_operation"]["enum"] == ["yes", "no", "unknown"]
        if with_skill:
            assert schema["properties"]["selected_skill_policy_ref"]["enum"] == [
                captured["checks"][identifier]["selected_skill_policy_ref"]]
            assert schema["properties"]["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
        else:
            assert "selected_skill_policy_ref" not in schema["properties"]
            assert "violates_selected_skill_policy" not in schema["properties"]
    goals = [unit for unit in primary["validation_task"]["units"] if unit["kind"] == "next_goal"]
    assert goals == []
    paths = primary["validation_task"]["program_future_paths"]
    assert paths == [{"parent_question_id": node()["id"], "parent_option_id": option_id,
        "chapter_id": "chapter1", "child_depth": 2, "child_goal": next_goal} for option_id in ("a", "b")]
    matrices = [json.loads(prompt) for prompt in service.prompts if "candidate_question" in json.loads(prompt)]
    assert [rule["decision_id"] for payload in matrices for rule in payload["fixed_decisions"]] == [
        rule["decision_id"] for rule in fixed]
    for payload in matrices:
        assert payload["candidate_question"] == captured["candidate"]["question"]
        for cell in matrix(payload["fixed_decisions"], payload["candidate_question"]).values():
            assert set(cell["options"]) == {"a", "b"}
            assert all(values == [0] * 5 for values in cell["options"].values())
    scope_calls = [call for call in service.model_calls if "checks" in call["json_schema"]["properties"]]
    assert {identifier for call in scope_calls
        for identifier in call["json_schema"]["properties"]["checks"]["properties"]} == set(review_units())
    assert len(service.model_calls) == len(scope_calls) + 2
    assert all(call["num_predict"] <= coordinator.MAX_SCOPE_OUTPUT_TOKENS for call in scope_calls)


@pytest.mark.parametrize("option_count", [2, 5])
@pytest.mark.asyncio
async def test_stored_writing_review_keeps_all_actual_options_and_complete_selected_skill_policy(option_count):
    record, original, fixed = fixture(24, option_count)
    record["topic"]["chapter_id"] = "chapter1"
    selected_skills = [{"id": "organize", "name": "章节组织", "description": "保持已有分类与数据处理政策",
        "executionSteps": [{"instruction": "逐条沿用当前行，不重新聚合。", "settings": {"aggregate": False}}]}]
    before = copy.deepcopy((record, original, selected_skills))
    expected_question = copy.deepcopy(record["question"])
    for field in ("context_details", "why_now", "single_choice_reason", "answer_template"):
        expected_question.pop(field, None)
    for option in expected_question["options"]:
        option.pop("details", None)
    checked = scope_checks(record, writing_organization=True, selected_skills=selected_skills)

    class WritingReviewService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            if "checks" in kwargs["json_schema"]["properties"]:
                schemas = kwargs["json_schema"]["properties"]["checks"]["properties"]
                response = {"checks": {identifier: checked["checks"][identifier] for identifier in schemas}}
            else:
                response = matrix(payload["fixed_decisions"], payload["candidate_question"])
            yield json.dumps(response, ensure_ascii=False)

    service = WritingReviewService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    result = await prepare(coordinator, stage="writing", kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original, selected_skills=selected_skills)
    assert result == {"review": "pass", "codes": [], "decision_ids": []}
    assert (record, original, selected_skills) == before
    skill_ref = checked["checks"]["q0_prompt"]["selected_skill_policy_ref"]
    scope_ids, rule_ids = [], []
    for call in service.model_calls:
        payload = json.loads(call["user_prompt"])
        if "checks" in call["json_schema"]["properties"]:
            assert payload["authority"]["user_constraints"]["selected_skills"] == selected_skills
            assert payload["authority"]["selected_skill_policy_binding"] == {
                "profiles_path": "authority.user_constraints.selected_skills", "profile_count": 1,
                "profiles_sha256": skill_ref}
            schemas = call["json_schema"]["properties"]["checks"]["properties"]
            scope_ids.extend(schemas)
            for schema in schemas.values():
                assert schema["properties"]["proposes_nonwriting_operation"]["enum"] == ["yes", "no", "unknown"]
                assert schema["properties"]["selected_skill_policy_ref"]["enum"] == [skill_ref]
                assert schema["properties"]["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
            assert payload["question_contexts"]["q0"]["question"] == expected_question
        else:
            assert payload["candidate_question"] == expected_question
            rule_ids.extend(rule["decision_id"] for rule in payload["fixed_decisions"])
            for schema in call["json_schema"]["properties"].values():
                assert set(schema["properties"]["options"]["properties"]) == {
                    option["id"] for option in record["question"]["options"]}
    assert scope_ids == ["q0_prompt", "q0_space"] + ["q0_o{}".format(index) for index in range(option_count)]
    assert rule_ids == [rule["decision_id"] for rule in fixed]


@pytest.mark.asyncio
async def test_new_protocol_checks_scope_and_matrix_for_every_review_question():
    record, original, fixed = fixture(7)
    second = copy.deepcopy(record)
    second["question"]["id"] += "-second"
    second["question"]["prompt"] = "保持全部既定内容，第二说明段如何组织？"
    responses = [matrix(fixed, record["question"]), matrix(fixed, second["question"])]
    checked = scope_checks(record)
    checked["checks"].update({identifier.replace("q0", "q1"): value
        for identifier, value in scope_checks(second)["checks"].items()})
    class ReviewMatrixService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.prompts.append(kwargs["user_prompt"])
            self.model_calls.append(kwargs)
            payload = json.loads(kwargs["user_prompt"])
            if "checks" in kwargs["json_schema"]["properties"]:
                response = {"checks": {identifier: checked["checks"][identifier]
                    for identifier in kwargs["json_schema"]["properties"]["checks"]["properties"]}}
            else:
                response = matrix(payload["fixed_decisions"], payload["candidate_question"])
            yield json.dumps(response, ensure_ascii=False)
    service = ReviewMatrixService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record), proof(second)]
    coordinator._check_new_decision_space = relationship
    result = await prepare(coordinator, kind="review", review_questions=[record, second],
        plan={"topics": [record["topic"]]}, decisions=original)
    assert result["review"] == "pass"
    payloads = [json.loads(prompt) for prompt in service.prompts]
    matrix_payloads = [payload for payload in payloads if "candidate_question" in payload]
    assert len(matrix_payloads) == 2
    assert matrix_payloads[0]["candidate_question"]["prompt"] == record["question"]["prompt"]
    assert matrix_payloads[1]["candidate_question"]["prompt"] == second["question"]["prompt"]
    scoped = [unit["id"] for payload in payloads if "question_contexts" in payload
        for unit in payload["validation_task"]["units"]]
    assert set(scoped) == set(checked["checks"]) and len(scoped) == len(checked["checks"])


@pytest.mark.asyncio
async def test_new_axis_transport_error_is_closed_without_exposing_provider_text(caplog):
    record, original, fixed = fixture(1)
    coordinator, service = setup([], check_consistency=True)
    async def broken_relationship(**kwargs):
        raise RuntimeError("private-provider-response")
    coordinator._check_new_decision_space = broken_relationship
    with pytest.raises(BrainstormGenerationError, match="checker_unavailable") as error:
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert "private-provider-response" not in str(error.value)
    assert "private-provider-response" not in caplog.text
    assert not service.model_calls


@pytest.mark.asyncio
async def test_missing_new_axis_proof_preserves_legacy_gate_and_never_uses_matrix():
    record, original, fixed = fixture(1)
    checked = check_result(unit_ids=review_units(), option_labels={
        option["id"]: option["label"] for option in record["question"]["options"]})
    coordinator, service = setup([checked], check_consistency=True)
    async def forbidden_matrix(**kwargs):
        pytest.fail("Legacy None relationship stub is not matrix authorization")
    coordinator._check_fixed_decision_matrix = forbidden_matrix
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=original)
    assert result["review"] == "pass" and len(service.model_calls) == 1


@pytest.mark.parametrize("invalid", [[], {}, [None]])
@pytest.mark.asyncio
async def test_invalid_present_relationship_proof_cannot_fall_back_to_selected_rule_subset(invalid):
    record, original, fixed = fixture(1)
    checked = check_result(unit_ids=review_units(), option_labels={
        option["id"]: option["label"] for option in record["question"]["options"]})
    coordinator, service = setup([checked], check_consistency=True)
    async def relationship(**kwargs):
        return invalid
    coordinator._check_new_decision_space = relationship
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert len(service.model_calls) == 0


@pytest.mark.asyncio
async def test_matrix_shares_existing_step_deadline_and_cancels_inflight_call(monkeypatch):
    record, original, fixed = fixture(1)
    checked = scope_checks(record)
    closed = asyncio.Event()
    class SlowMatrixService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            if len(self.model_calls) == 1:
                yield json.dumps(checked, ensure_ascii=False)
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = SlowMatrixService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.04)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert closed.is_set()
    assert 1200 <= service.model_calls[0]["num_predict"] <= BrainstormStageCoordinator.MAX_SCOPE_OUTPUT_TOKENS
    assert 1200 <= service.model_calls[1]["num_predict"] <= BrainstormStageCoordinator.MAX_SCOPE_OUTPUT_TOKENS
