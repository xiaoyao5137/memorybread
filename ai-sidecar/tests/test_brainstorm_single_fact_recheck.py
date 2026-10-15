"""A disputed fact cannot turn into a second unrelated compatibility review."""
import asyncio
import copy
import hashlib
import json

import pytest

from creation.brainstorm import BrainstormGenerationError, BrainstormGenerationTimeout
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.test_brainstorm_fixed_matrix import FACTS, MODEL, fixture, proof, scope_checks
from tests.test_brainstorm_stage_nodes import prepare
from tests.test_creation_brainstorm import StubCreationService

FACT_NAMES = FACTS + ["repeats_fixed_choice"]


def snapshot(fixed):
    return hashlib.sha256(json.dumps(fixed, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def inherited(fixed):
    return {"snapshot_sha256": snapshot(fixed), "decision_ids": [rule["decision_id"] for rule in fixed],
        "application": "mandatory_preserved_base", "batch_rule_ids": [rule["decision_id"] for rule in fixed],
        "batch_rules_path": "fixed_decisions"}


def requested(rule="decision_1", option="b", fact=FACTS[0]):
    return {"decision_id": rule, "option_id": option, "fact_name": fact}


class FactService(StubCreationService):
    def __init__(self, verdicts=None, mutate=None):
        super().__init__([])
        self.verdicts = verdicts or {}
        self.mutate = mutate

    async def _stream_direct_completion(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.model_calls.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        records = {}
        for pair in payload["pairs"]:
            schema = kwargs["json_schema"]["properties"][pair["pair_id"]]["properties"]
            records[pair["pair_id"]] = {"fixed_evidence": schema["fixed_evidence"]["enum"][0],
                "candidate_evidence": schema["candidate_evidence"]["enum"][-1],
                "fact_name": pair["fact_name"], "verdict": self.verdicts.get(pair["pair_id"], "no")}
        if self.mutate:
            self.mutate(records)
        yield json.dumps(records, ensure_ascii=False)


async def recheck(fixed, record, pairs, service=None, *, evidence=None, base=None):
    service = service or FactService()
    coordinator = Coordinator(service)
    result = await coordinator._recheck_fixed_matrix_changes(question=record["question"], fixed=fixed,
        context={"selected_topic": record["topic"], "node_goal": "比较局部组织方法", "ancestor_conditions": []},
        pairs=pairs, evidence=evidence if evidence is not None else {rule["decision_id"]: [rule["answer"]] for rule in fixed},
        inherited_fixed_authority=base if base is not None else inherited(fixed), **MODEL)
    return result, service


@pytest.mark.parametrize("fact", FACT_NAMES)
@pytest.mark.asyncio
async def test_only_requested_fact_has_a_verdict_and_exact_complete_sources(fact):
    record, original, fixed = fixture()
    pairs = [requested(fact=fact)]
    before = copy.deepcopy((record, fixed, pairs))
    result, service = await recheck(fixed, record, pairs)
    assert result == [{**pairs[0], "verdict": "no"}]
    assert (record, fixed, pairs) == before and len(service.model_calls) == 1
    payload = json.loads(service.prompts[0])
    pair = payload["pairs"][0]
    assert pair["fixed_decision"] == fixed[0] and pair["actual_option"] == record["question"]["options"][1]
    assert payload["candidate_question"] == record["question"]
    assert payload["validation_task"]["requested_facts"] == [fact]
    assert set(payload["validation_task"]["fact_definitions"]) == {fact}
    assert payload["comparison_contract"]["requested_facts_only"] is True
    assert payload["inherited_fixed_authority"]["snapshot_sha256"] == snapshot(fixed)
    assert payload["inherited_fixed_authority"]["decision_ids"] == [rule["decision_id"] for rule in fixed]
    assert payload[payload["inherited_fixed_authority"]["batch_rules_path"]] == [fixed[0]]
    schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
    assert set(schema) == {"fixed_evidence", "candidate_evidence", "fact_name", "verdict"}
    policy = json.loads(schema["fixed_evidence"]["enum"][0])
    assert policy["object_context"] == {"question": fixed[0]["question"], "role": "scope_only_not_confirmation"}
    assert policy["effective_user_policy"]["current_answer"] == fixed[0]["answer"]
    assert policy["effective_user_policy"]["user_inputs"] == fixed[0]["user_inputs"]
    assert schema["candidate_evidence"]["enum"] == ["\n".join(pair["actual_option"][field]
        for field in ("label", "description", "tradeoff")
        if isinstance(pair["actual_option"].get(field), str) and pair["actual_option"][field].strip())]
    assert schema["fact_name"]["enum"] == [fact] and service.model_calls[0]["temperature"] == 0.0


@pytest.mark.parametrize("fact", FACT_NAMES)
@pytest.mark.asyncio
async def test_requested_true_fact_rejects_precise_rule_with_the_appropriate_code(fact):
    record, original, fixed = fixture()
    code = "confirmed_decision_reopened" if fact == "repeats_fixed_choice" else "confirmed_decision_conflict"
    with pytest.raises(BrainstormGenerationError, match=code) as error:
        await recheck(fixed, record, [requested("decision_10", fact=fact)], FactService({"pair_0": "yes"}))
    assert error.value.decision_ids == ["decision_10"]


@pytest.mark.parametrize("fact", FACT_NAMES)
@pytest.mark.asyncio
async def test_unknown_for_the_requested_fact_remains_blocking(fact):
    record, original, fixed = fixture(1)
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await recheck(fixed, record, [requested(fact=fact)], FactService({"pair_0": "unknown"}))


@pytest.mark.parametrize("mutation", [
    lambda checked: checked.pop("pair_0"),
    lambda checked: checked.update(forged=copy.deepcopy(checked["pair_0"])),
    lambda checked: checked["pair_0"].update(fixed_evidence="候选虚构的旧规则"),
    lambda checked: checked["pair_0"].update(candidate_evidence="候选没有这个行为"),
    lambda checked: checked["pair_0"].pop("candidate_evidence"),
    lambda checked: checked["pair_0"].pop("fact_name"),
    lambda checked: checked["pair_0"].update(fact_name=FACTS[1]),
    lambda checked: checked["pair_0"].update(fact_name="invented_fact"),
    lambda checked: checked["pair_0"].pop("verdict"),
    lambda checked: checked["pair_0"].update(verdict="pass"),
    lambda checked: checked["pair_0"].update(verdict=False),
    lambda checked: checked["pair_0"].update(facts=["unknown"] * 4),
    lambda checked: checked["pair_0"].update(policy_relation="other"),
    lambda checked: checked["pair_0"].update(comparison_basis="policy_proposal"),
    lambda checked: checked["pair_0"].update(decision_id="decision_2"),
    lambda checked: checked["pair_0"].update(option_id="a"),
])
@pytest.mark.asyncio
async def test_unrequested_facts_fake_sources_and_extra_verdicts_are_closed(mutation):
    record, original, fixed = fixture(1)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, [requested()], FactService(mutate=mutation))


@pytest.mark.parametrize("pairs", [None, [], [None], [{}],
    [requested(fact="invented_fact")], [requested(rule="decision_99")], [requested(option="wrong")],
    [{"decision_id": "decision_1", "option_id": "a"}], [{**requested(), "pair_id": "user_chosen"}],
    [requested(), requested()], [{**requested(), "fact_name": []}],
])
@pytest.mark.asyncio
async def test_pairs_require_exact_actual_rule_option_and_finite_fact(pairs):
    record, original, fixed = fixture(1)
    service = FactService()
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, pairs, service)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_distinct_requested_facts_on_the_same_real_pair_are_all_preserved():
    record, original, fixed = fixture(1)
    pairs = [requested(fact=fact) for fact in FACT_NAMES]
    result, service = await recheck(fixed, record, pairs)
    assert result == [{**pair, "verdict": "no"} for pair in pairs]
    assert sum(len(json.loads(prompt)["pairs"]) for prompt in service.prompts) == len(pairs)


@pytest.mark.asyncio
async def test_later_true_compatibility_change_is_not_hidden_by_an_earlier_repeat():
    record, original, fixed = fixture()
    pairs = [requested("decision_{}".format(index), fact="repeats_fixed_choice") for index in range(1, 7)]
    pairs.append(requested("decision_10", fact=FACTS[3]))
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await recheck(fixed, record, pairs, FactService({"pair_0": "yes", "pair_6": "yes"}))
    assert error.value.decision_ids == ["decision_10"]


@pytest.mark.asyncio
async def test_false_carrier_and_trigger_changes_clear_only_their_exact_facts():
    record, original, fixed = fixture()
    fixed[2].update(answer="趋势图采用折线图", manually_edited=True)
    fixed[3].update(answer="数据缺失时标注数据缺失并暂停该指标趋势分析", manually_edited=True)
    fixed[9].update(answer="缺失值使用灰色‘—’，附注数据源未提供", manually_edited=True)
    record["question"]["options"][1]["description"] = "在既定折线图中，缺失值改为留空，附注数据源未提供"
    record["question"]["options"][1]["label"] = "留白+底部附注"
    record["question"]["options"][1]["tradeoff"] = "保留折线图载体，缺失位置用留白方式呈现"
    pairs = [requested("decision_3", fact=FACTS[3]), requested("decision_4", fact=FACTS[0])]
    result, service = await recheck(fixed, record, pairs)
    assert result == [{**pair, "verdict": "no"} for pair in pairs]
    with pytest.raises(BrainstormGenerationError, match="confirmed_decision_conflict") as error:
        await recheck(fixed, record, [requested("decision_10", fact=FACTS[3])], FactService({"pair_0": "yes"}))
    assert error.value.decision_ids == ["decision_10"]
    for prompt in service.prompts:
        assert "repeats_fixed_choice" not in json.loads(prompt)["validation_task"]["fact_definitions"]
    for call in service.model_calls:
        for schema in call["json_schema"]["properties"].values():
            assert schema["properties"]["candidate_evidence"]["enum"] == [
                "留白+底部附注\n在既定折线图中，缺失值改为留空，附注数据源未提供\n保留折线图载体，缺失位置用留白方式呈现"]


@pytest.mark.parametrize("field", ["label", "description", "tradeoff"])
@pytest.mark.asyncio
async def test_one_option_field_cannot_hide_the_complete_carrier_and_missing_value_method(field):
    record, original, fixed = fixture()
    fixed[2].update(answer="趋势图采用折线图", manually_edited=True)
    option = record["question"]["options"][1]
    option.update(label="留白+底部附注", description="在折线图中将缺失值位置留空，底部附注数据源未提供",
        tradeoff="继续使用折线图；局部缺失标记改为留白")
    service = FactService(mutate=lambda checked: checked["pair_0"].update(candidate_evidence=option[field]))
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, [requested("decision_3", fact=FACTS[3])], service)
    payload = json.loads(service.prompts[0])
    assert payload["pairs"][0]["actual_option"] == option
    assert payload["pairs"][0]["fixed_decision"] == fixed[2]


@pytest.mark.parametrize("tradeoff", [None, "", "  ", "完整取舍"])
@pytest.mark.asyncio
async def test_complete_candidate_evidence_includes_only_effective_actual_tradeoff(tradeoff):
    record, original, fixed = fixture(1)
    option = record["question"]["options"][1]
    option["tradeoff"] = tradeoff
    result, service = await recheck(fixed, record, [requested()])
    expected = option["label"] + "\n" + option["description"]
    if isinstance(tradeoff, str) and tradeoff.strip():
        expected += "\n" + tradeoff
    schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
    assert schema["candidate_evidence"]["enum"] == [expected]
    assert result[0]["verdict"] == "no"
    assert json.loads(service.prompts[0])["candidate_evidence_refs"] == {}


@pytest.mark.asyncio
async def test_complete_old_policy_keeps_local_object_and_all_effective_sources_in_one_certificate():
    record, original, fixed = fixture(1)
    fixed[0].update(question="若采用结论证据成对清单，清单内的信息怎样排列？", answer="结论编号先行",
        user_inputs=["结论编号先行", "仅规定结论证据成对清单，保持其它段落组织自由"],
        selected_options=[{"id": "a", "label": "结论编号先行", "description": "在结论证据成对清单内安排信息顺序",
            "tradeoff": "先列结论编号与原结论，再列证据编号、来源定位及限制说明"}])
    record["question"].update(prompt="量化结果之后，周期与来源说明如何组织？")
    record["question"]["options"][1].update(label="先介绍来源再交代周期",
        description="保留整体成果优先及全部原要求，仅组织成果后的背景说明")
    before = copy.deepcopy(fixed)
    result, service = await recheck(fixed, record, [requested(fact=FACTS[3])])
    assert result[0]["verdict"] == "no" and fixed == before
    schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
    assert len(schema["fixed_evidence"]["enum"]) == 1
    policy = json.loads(schema["fixed_evidence"]["enum"][0])
    assert policy == {"object_context": {"question": fixed[0]["question"], "role": "scope_only_not_confirmation"},
        "effective_user_policy": {"answer_source": "user", "current_answer": "结论编号先行",
            "user_inputs": fixed[0]["user_inputs"], "manually_edited": False,
            "current_answer_precedence": "current_answer_and_user_limits_override_conflicting_selected_text",
            "effective_selected_explanations": [{"description": fixed[0]["selected_options"][0]["description"],
                "tradeoff": fixed[0]["selected_options"][0]["tradeoff"]}]}}
    payload = json.loads(service.prompts[0])
    assert payload["pairs"][0]["fixed_decision"] == fixed[0]
    assert payload["pairs"][0]["actual_option"] == record["question"]["options"][1]


@pytest.mark.parametrize("mutation", ["short_answer", "question_only", "selected_only", "missing_object", "candidate_scope"])
@pytest.mark.asyncio
async def test_old_policy_evidence_cannot_omit_object_or_choose_one_convenient_source(mutation):
    record, original, fixed = fixture(1)
    def edit(checked):
        if mutation == "short_answer":
            checked["pair_0"]["fixed_evidence"] = fixed[0]["answer"]
        elif mutation == "question_only":
            checked["pair_0"]["fixed_evidence"] = fixed[0]["question"]
        elif mutation == "selected_only":
            checked["pair_0"]["fixed_evidence"] = fixed[0]["selected_options"][0]["description"]
        else:
            policy = json.loads(checked["pair_0"]["fixed_evidence"])
            if mutation == "missing_object":
                policy.pop("object_context")
            else:
                policy["object_context"]["question"] = record["question"]["prompt"]
            checked["pair_0"]["fixed_evidence"] = json.dumps(policy, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, [requested()], FactService(mutate=edit))


@pytest.mark.asyncio
async def test_edited_policy_has_current_answer_and_full_user_text_without_reviving_selected_explanations():
    record, original, fixed = fixture(1)
    fixed[0].update(question="说明段原有的组织顺序怎样安排？", answer="来源定位放最后",
        manually_edited=True, user_inputs=["原先选择定位放开头", "还须保留原值，禁止新增计算"],
        selected_options=[{"description": "定位放开头", "tradeoff": "旧方法：先定位后解释"}])
    result, service = await recheck(fixed, record, [requested()])
    assert result[0]["verdict"] == "no"
    policy = json.loads(service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]["fixed_evidence"]["enum"][0])
    assert policy["effective_user_policy"]["current_answer"] == "来源定位放最后"
    assert policy["effective_user_policy"]["user_inputs"] == fixed[0]["user_inputs"]
    assert policy["effective_user_policy"]["effective_selected_explanations"] == []
    assert policy["effective_user_policy"]["current_answer_precedence"] == "current_answer_controls_revision_and_conflicting_prior_text"
    assert json.loads(service.prompts[0])["pairs"][0]["fixed_decision"]["selected_options"] == fixed[0]["selected_options"]


@pytest.mark.asyncio
async def test_complete_policy_retains_normal_negative_user_constraints_and_does_not_confirm_question_assumptions():
    record, original, fixed = fixture(1)
    fixed[0].update(question="若已有足够样本，报告如何组织？", answer="不使用交互展示，只整理现有原值",
        user_inputs=["不要新增指标计算", "不要按领域聚合"], manually_edited=True)
    policy = json.loads(Coordinator._fixed_policy_evidence(fixed[0]))
    assert policy["object_context"] == {"question": fixed[0]["question"], "role": "scope_only_not_confirmation"}
    assert policy["effective_user_policy"]["current_answer"] == fixed[0]["answer"]
    assert policy["effective_user_policy"]["user_inputs"] == fixed[0]["user_inputs"]
    assert "足够样本" not in json.dumps(policy["effective_user_policy"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_empty_exclusion_policy_binds_actual_excluded_scope_as_exclusion_not_confirmation():
    record, original, fixed = fixture(1)
    fixed[0].update(answer_source="user_excluded", answer="", excluded_topic="新增交互展示",
        question="是否新增交互展示？", user_inputs=["排除该形式"], selected_options=[{"tradeoff": "旧候选并非确认"}])
    result, service = await recheck(fixed, record, [requested()],
        evidence={"decision_1": [fixed[0]["excluded_topic"], fixed[0]["question"]]})
    assert result[0]["verdict"] == "no"
    policy = json.loads(service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]["fixed_evidence"]["enum"][0])
    effective = policy["effective_user_policy"]
    assert effective["answer_source"] == "user_excluded" and effective["current_answer"] == ""
    assert effective["excluded_topic"] == fixed[0]["excluded_topic"]
    assert effective["interpretation"] == "exclude_object_not_confirm_question"
    assert effective["effective_selected_explanations"] == []


@pytest.mark.parametrize("mutation", [
    lambda rule: rule.update(answer_source="model_assumption"),
    lambda rule: rule.update(question={"fake": "object"}),
    lambda rule: rule.update(user_inputs="not_actual_user_list"),
    lambda rule: rule.update(user_inputs=[{"invented": "input"}]),
    lambda rule: rule.update(selected_options=["not_selected_option"]),
    lambda rule: rule.update(manually_edited="false"),
])
def test_effective_policy_compilation_rejects_malformed_or_nonuser_authority(mutation):
    record, original, fixed = fixture(1)
    mutation(fixed[0])
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._fixed_policy_evidence(fixed[0])


@pytest.mark.parametrize("manually_edited", [False, True])
@pytest.mark.asyncio
async def test_full_current_rule_bounds_evidence_and_edited_answers_override_old_selected_text(manually_edited):
    record, original, fixed = fixture(1)
    fixed[0]["manually_edited"] = manually_edited
    selected = fixed[0]["selected_options"][0]["description"]
    evidence = {"decision_1": [selected]}
    if manually_edited:
        with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
            await recheck(fixed, record, [requested()], evidence=evidence)
    else:
        result, service = await recheck(fixed, record, [requested()], evidence=evidence)
        assert result[0]["verdict"] == "no"
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, [requested()], evidence={"decision_1": [fixed[0]["question"]]})


@pytest.mark.asyncio
async def test_empty_explicit_exclusion_uses_its_actual_source_without_confirming_question_assumptions():
    record, original, fixed = fixture(1)
    fixed[0].update(answer="", answer_source="user_excluded", excluded_topic="个人资料读取")
    result, service = await recheck(fixed, record, [requested(fact=FACTS[2])],
        evidence={"decision_1": [fixed[0]["excluded_topic"], fixed[0]["question"]]})
    assert result[0]["verdict"] == "no"
    assert json.loads(service.prompts[0])["pairs"][0]["fixed_decision"] == fixed[0]


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.asyncio
async def test_every_requested_fact_is_in_a_bounded_complete_batch(large):
    record, original, fixed = fixture(24, 5)
    if large:
        for rule in fixed:
            rule["answer"] += "真实完整固定对象与实际作用范围。" * 12
        for option in record["question"]["options"]:
            option["description"] += "实际局部方法完整保留。" * 8
    pairs = [requested(rule["decision_id"], option["id"], fact) for rule in fixed[:6]
        for option in record["question"]["options"] for fact in FACTS]
    result, service = await recheck(fixed, record, pairs)
    assert result == [{**pair, "verdict": "no"} for pair in pairs]
    actual = []
    for prompt, call in zip(service.prompts, service.model_calls):
        payload = json.loads(prompt)
        actual.extend({field: pair[field] for field in ("decision_id", "option_id", "fact_name")} for pair in payload["pairs"])
        assert 1 <= len(payload["pairs"]) <= 6
        assert 1200 <= call["num_predict"] <= Coordinator.MAX_SCOPE_OUTPUT_TOKENS
        assert payload["inherited_fixed_authority"]["decision_ids"] == [rule["decision_id"] for rule in fixed]
        assert payload["inherited_fixed_authority"]["snapshot_sha256"] == snapshot(fixed)
    assert actual == pairs


@pytest.mark.parametrize("mutation", [None, "fake", "stale"])
@pytest.mark.asyncio
async def test_large_original_policy_uses_a_complete_current_rule_hash_without_truncation(mutation):
    record, original, fixed = fixture(1)
    fixed[0].update(answer="完整真实规则" * 3000, manually_edited=True)
    def edit(checked):
        if mutation:
            other = copy.deepcopy(fixed[0])
            other["answer"] = "过期规则" if mutation == "stale" else "候选造出的原规则"
            checked["pair_0"]["fixed_evidence"] = "policy:" + snapshot([other])
    service = FactService(mutate=edit)
    if mutation:
        with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
            await recheck(fixed, record, [requested()], service)
    else:
        result, service = await recheck(fixed, record, [requested()], service)
        assert result[0]["verdict"] == "no"
        payload = json.loads(service.prompts[0])
        assert payload["pairs"][0]["fixed_decision"] == fixed[0]
        digest = hashlib.sha256(json.dumps(fixed[0], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
        assert schema["fixed_evidence"]["enum"] == ["policy:" + digest]
        assert payload["candidate_evidence_refs"] == {}
        assert 1200 <= service.model_calls[0]["num_predict"] <= Coordinator.MAX_SCOPE_OUTPUT_TOKENS


@pytest.mark.parametrize("stale_object", [False, True])
@pytest.mark.asyncio
async def test_long_object_and_user_inputs_bind_the_whole_actual_rule_hash_not_only_the_answer(stale_object):
    record, original, fixed = fixture(1)
    fixed[0].update(question="此顺序仅适用于原对象与以下完整语境：" + "原对象的完整作用域。" * 1000,
        user_inputs=["保持全部原值与原方法。" * 1000])
    def edit(checked):
        if stale_object:
            old = copy.deepcopy(fixed[0])
            old["question"] = "过期的另一个对象"
            digest = hashlib.sha256(json.dumps(old, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            checked["pair_0"]["fixed_evidence"] = "policy:" + digest
    service = FactService(mutate=edit)
    if stale_object:
        with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
            await recheck(fixed, record, [requested()], service)
    else:
        result, service = await recheck(fixed, record, [requested()], service)
        assert result[0]["verdict"] == "no"
        payload = json.loads(service.prompts[0])
        assert payload["pairs"][0]["fixed_decision"] == fixed[0]
        digest = hashlib.sha256(json.dumps(fixed[0], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        quote = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]["fixed_evidence"]["enum"]
        assert quote == ["policy:" + digest]
        assert payload["fixed_evidence_refs"]["pair_0"]["evidence_ref"] == quote[0]


@pytest.mark.parametrize("mutation", [None, "fake", "stale", "short_label"])
@pytest.mark.asyncio
async def test_large_candidate_uses_the_complete_actual_option_hash_without_truncation(mutation):
    record, original, fixed = fixture(1)
    option = record["question"]["options"][1]
    option.update(label="保留折线图载体", description="完整实际选项：在折线图中采用留白方式呈现缺失值。" * 1000,
        tradeoff="仍使用折线图；实际变化为缺失标记")
    def edit(checked):
        if mutation == "short_label":
            checked["pair_0"]["candidate_evidence"] = option["label"]
        elif mutation:
            other = copy.deepcopy(option)
            other["description"] = "过期选项" if mutation == "stale" else "模型虚构的候选行为"
            digest = hashlib.sha256(json.dumps(other, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            checked["pair_0"]["candidate_evidence"] = "option:" + digest
    service = FactService(mutate=edit)
    if mutation:
        with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
            await recheck(fixed, record, [requested()], service)
    else:
        result, service = await recheck(fixed, record, [requested()])
        assert result[0]["verdict"] == "no"
        payload = json.loads(service.prompts[0])
        assert payload["pairs"][0]["actual_option"] == option
        assert payload["candidate_question"] == record["question"]
        digest = hashlib.sha256(json.dumps(option, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        ref = "option:" + digest
        schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
        assert schema["candidate_evidence"]["enum"] == [ref]
        assert schema["fixed_evidence"]["enum"] == [Coordinator._fixed_policy_evidence(fixed[0])]
        assert payload["candidate_evidence_refs"] == {"pair_0": {"evidence_ref": ref, "option_id": option["id"]}}
        assert payload["fixed_evidence_refs"] == {}
        assert 1200 <= service.model_calls[0]["num_predict"] <= Coordinator.MAX_SCOPE_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_hash_fallback_is_only_for_a_single_pair_that_exceeds_the_bounded_output():
    record, original, fixed = fixture(2)
    option = record["question"]["options"][1]
    option["description"] = "实际完整局部组织方案。" * 80
    pairs = [requested(rule["decision_id"]) for rule in fixed]
    result, service = await recheck(fixed, record, pairs)
    assert result == [{**pair, "verdict": "no"} for pair in pairs]
    assert len(service.model_calls) == 2
    for prompt, call in zip(service.prompts, service.model_calls):
        payload = json.loads(prompt)
        assert len(payload["pairs"]) == 1
        assert payload["candidate_evidence_refs"] == payload["fixed_evidence_refs"] == {}
        expected = "\n".join(option[field] for field in ("label", "description", "tradeoff")
            if isinstance(option.get(field), str) and option[field].strip())
        for schema in call["json_schema"]["properties"].values():
            assert schema["properties"]["candidate_evidence"]["enum"] == [expected]


@pytest.mark.asyncio
async def test_large_policy_and_candidate_both_remain_complete_when_both_need_hash_refs():
    record, original, fixed = fixture(1)
    fixed[0].update(answer="完整真实固定规则。" * 1000, manually_edited=True)
    record["question"]["options"][1]["description"] = "完整实际候选说明。" * 1000
    result, service = await recheck(fixed, record, [requested()])
    assert result[0]["verdict"] == "no"
    payload = json.loads(service.prompts[0])
    assert payload["pairs"][0]["fixed_decision"] == fixed[0]
    assert payload["pairs"][0]["actual_option"] == record["question"]["options"][1]
    schema = service.model_calls[0]["json_schema"]["properties"]["pair_0"]["properties"]
    for field, prefix, actual in (("fixed_evidence", "policy:", fixed[0]),
            ("candidate_evidence", "option:", record["question"]["options"][1])):
        digest = hashlib.sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        assert schema[field]["enum"] == [prefix + digest]


@pytest.mark.parametrize("mutation", [
    lambda base: base.update(snapshot_sha256="0" * 64),
    lambda base: base.update(snapshot_sha256="not_sha"),
    lambda base: base.update(decision_ids=["decision_2"]),
    lambda base: base.update(decision_ids=["decision_1", "decision_1"]),
    lambda base: base.update(application="candidate_claims_preserved"),
])
@pytest.mark.asyncio
async def test_inherited_authority_cannot_be_replaced_by_a_candidate_claim(mutation):
    record, original, fixed = fixture(1)
    base = inherited(fixed)
    mutation(base)
    service = FactService()
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await recheck(fixed, record, [requested()], service, base=base)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_single_fact_recheck_is_cancelled_by_the_existing_shared_stage_deadline(monkeypatch):
    record, original, fixed = fixture(1)
    closed = asyncio.Event()
    class SlowFactService(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            if "checks" in kwargs["json_schema"]["properties"]:
                yield json.dumps(scope_checks(record), ensure_ascii=False)
            elif "pairs" not in json.loads(kwargs["user_prompt"]):
                response = {identifier: {"fixed_evidence": schema["properties"]["fixed_evidence"]["enum"][0],
                    "options": {option_id: [0] * option_schema["maxItems"]
                        for option_id, option_schema in schema["properties"]["options"]["properties"].items()}}
                    for identifier, schema in kwargs["json_schema"]["properties"].items()}
                response["decision_1"]["options"]["b"][0] = 1
                yield json.dumps(response, ensure_ascii=False)
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = SlowFactService([])
    coordinator = Coordinator(service)
    async def relationship(**kwargs):
        return [proof(record)]
    coordinator._check_new_decision_space = relationship
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(Coordinator, "MAX_TURN_SECONDS", 0.06)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=original)
    assert closed.is_set() and len(service.model_calls) == 3
    assert service.model_calls[-1]["temperature"] == 0.0
