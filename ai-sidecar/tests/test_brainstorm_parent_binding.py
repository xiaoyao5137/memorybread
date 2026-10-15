"""Actual hypothetical parents are evidence, never automatic NL admission."""
import copy
import json

import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator, _StageConsistencyError
from tests.test_brainstorm_editorial_integration import capture_compilation
from tests.test_brainstorm_fixed_matrix import decisions, proof
from tests.test_brainstorm_stage_nodes import setup


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}


def ordinary_case(option_count=2):
    ancestors = [{"parent_question_id": "parent", "parent_option_id": "a",
        "parent_prompt": "怎样组织新增说明？", "option_label": "先定义再举例",
        "option_description": "定义在前，例子在后；全部既定内容与方法保持不变。"}]
    question = {"id": "child", "prompt": "若先定义再举例，例子内部怎样展开？",
        "type": "single_choice", "parent_question_id": "parent", "parent_option_id": "a",
        "options": [{"id": chr(ord("a") + index), "label": "说明方法{}".format(index + 1),
            "description": "仅细化例子内部说明{}，定义仍在前、例子仍在后。".format(index + 1)}
            for index in range(option_count)]}
    topic = {"id": "topic", "title": "例子说明", "goal": "组织例子内部说明"}
    task = {"fixed_decisions": [], "cleared_constraints": [], "user_constraints": {},
        "ancestor_conditions": ancestors, "goal": topic["goal"]}
    return {"stage": "detail", "kind": "node", "topic": topic, "task": task,
        "result": {"question": question, "next_goals": []}, "other_topics": []}


def source_context(case):
    candidate = Coordinator._consistency_candidate(case["result"])
    units, contexts = Coordinator._consistency_units(candidate, case["topic"], case["task"])
    units = [{**unit, "kind": "choice_scope"} if unit["kind"] == "decision_space" else unit for unit in units]
    context = contexts["q0"]
    context["effective_goal"] = case["task"]["goal"]
    context["effective_goal_source"] = "node_goal"
    return units, context


def safe_response(schema, parent="preserves"):
    checks = {}
    for identifier, record in schema["properties"]["checks"]["properties"].items():
        checks[identifier] = {field: "no" if "no" in value["enum"] else
            parent if field == "parent" and "preserves" in value["enum"] else value["enum"][0]
            for field, value in record["properties"].items()}
    return {"checks": checks}


async def run_scope(case, *, parent="preserves", mutate=None):
    coordinator, service = setup([], check_consistency=True)
    scopes = []
    matrices = []
    async def novelty(**kwargs):
        candidate = kwargs["candidate"]
        return [proof({"question": candidate["question"]})]
    async def completion(prompt, **kwargs):
        payload = json.loads(prompt)
        scopes.append((payload, copy.deepcopy(kwargs)))
        response = safe_response(kwargs["json_schema"], parent)
        if mutate is not None:
            mutate(payload, kwargs, response)
        return json.dumps(response, ensure_ascii=False)
    async def matrix(**kwargs):
        matrices.append(copy.deepcopy(kwargs))
    coordinator._check_new_decision_space = novelty
    coordinator._complete = completion
    coordinator._check_fixed_decision_matrix = matrix
    await Coordinator._check_stage_consistency(coordinator, **{**MODEL, **case})
    return coordinator, service, scopes, matrices


async def finite_child():
    selected = [{"key": "complete-policy", "executionSteps": [{"objective": "既定内容和证据位置保持不变"}]}]
    _, _, root_task, root = await capture_compilation(decisions=decisions(), selected_skills=selected)
    option = root["question"]["options"][0]
    condition = {"parent_question_id": root["question"]["id"], "parent_option_id": option["id"],
        "parent_prompt": root["question"]["prompt"], "option_label": option["label"],
        "option_description": option["description"]}
    node = {"id": "compiled-child", "goal": Coordinator.WRITING_AXES[0]["levels"][1], "depth": 2,
        "parent_question_id": condition["parent_question_id"], "parent_option_id": option["id"],
        "ancestor_conditions": [condition], "max_options": 2, "max_next_goals": 0}
    _, _, child_task, result = await capture_compilation(node=node, decisions=decisions(), selected_skills=selected)
    return child_task, result, root_task, root


def test_every_ordinary_unit_is_bound_to_full_actual_parent_and_full_actual_question():
    case = ordinary_case(5)
    units, context = source_context(case)
    bindings = [Coordinator._parent_scope_binding(unit, context) for unit in units]
    assert len(bindings) == 7
    assert all(binding["ancestor_conditions"] == case["task"]["ancestor_conditions"] for binding in bindings)
    assert all(binding["actual_unit"]["question_sha256"] == Coordinator._scope_snapshot_sha(context["question"])
        for binding in bindings)
    for unit, binding in zip(units, bindings):
        schema = Coordinator._bound_scope_schema(unit, context, case["task"], {"pass": ("none", [])})
        assert schema["properties"]["parent_evidence_ref"]["enum"] == [Coordinator._scope_snapshot_sha(binding)]
        assert schema["properties"]["parent"]["enum"] == ["preserves", "violates", "unknown"]
        if unit["kind"] == "option":
            option = context["question"]["options"][unit["option_index"]]
            assert binding["actual_unit"]["candidate_text"] == option["label"] + "\n" + option["description"]


@pytest.mark.parametrize("field", ["parent_prompt", "option_label", "option_description", "parent_option_id"])
def test_parent_ref_cannot_reuse_another_real_condition(field):
    case = ordinary_case()
    units, context = source_context(case)
    unit = units[-1]
    old = Coordinator._scope_snapshot_sha(Coordinator._parent_scope_binding(unit, context))
    context["ancestor_conditions"][0][field] += "其它"
    if field == "parent_option_id":
        context["question"]["parent_option_id"] = context["ancestor_conditions"][0][field]
    schema = Coordinator._bound_scope_schema(unit, context, case["task"], {"pass": ("none", [])})
    cell = safe_response({"properties": {"checks": {"properties": {unit["id"]: schema}}}})["checks"]
    cell[unit["id"]]["parent_evidence_ref"] = old
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        Coordinator._bound_scope_verdicts(cell, {unit["id"]: schema})


def test_free_next_goal_binds_the_complete_actual_branch_and_all_ancestors():
    case = ordinary_case()
    units, context = source_context(case)
    unit = {"id": "g1_0", "kind": "next_goal", "context_key": "q0",
        "text": "安排说明细节", "branch_option_id": "b"}
    binding = Coordinator._parent_scope_binding(unit, context)
    assert binding["branch_condition"] == {"question_id": "child", "question_prompt": context["question"]["prompt"],
        "option": context["question"]["options"][1]}
    assert binding["ancestor_conditions"] == context["ancestor_conditions"]
    unit["branch_option_id"] = "unknown"
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        Coordinator._parent_scope_binding(unit, context)


@pytest.mark.asyncio
async def test_full_finite_witness_covers_all_actual_options_but_does_not_change_matrix_context():
    case, _, _, _ = await finite_child()
    before = copy.deepcopy(case)
    _, service, scopes, matrices = await run_scope(case)
    covered = []
    for payload, kwargs in scopes:
        context = payload["question_contexts"]["q0"]
        assert kwargs["output_tokens"] <= 4000 and kwargs["temperature"] == 0.0
        schemas = kwargs["json_schema"]["properties"]["checks"]["properties"]
        for unit in payload["validation_task"]["units"]:
            binding = context["parent_bindings"][unit["id"]]
            assert schemas[unit["id"]]["properties"]["parent_evidence_ref"]["enum"] == [Coordinator._scope_snapshot_sha(binding)]
            witness = context["editorial_parent_witnesses"][unit["id"]]
            assert witness["compiler_inputs_sha256"] == Coordinator._scope_snapshot_sha(case["task"]["editorial_compilation"]["inputs"])
            assert witness["compiler_packet_sha256"] == Coordinator._scope_snapshot_sha(case["task"]["editorial_compilation"]["packet"])
            assert witness["actual_parent_binding_sha256"] == Coordinator._scope_snapshot_sha(binding)
            assert witness["requires_semantic_review"] is True
            assert "compiled_packet" not in witness and "authority" not in witness and "inputs" not in witness
            assert set(witness["interpretations"]) == ({unit["option_id"]} if unit["kind"] == "option" else {"a", "b"})
            for interpretation in witness["interpretations"].values():
                assert [node["kind"] for node in interpretation["new_text_nodes"]] == ["new_overview", "new_explanation"]
                assert interpretation["refinements"][0]["targets"][0]["path"] == [1, "text"]
                assert interpretation["ancestor_projections"][0]["expected_nodes"] == interpretation["ancestor_projections"][0]["projected_nodes"]
            covered.append(unit["id"])
    assert set(covered) == {"q0_prompt", "q0_space", "q0_o0", "q0_o1"}
    assert len(matrices) == 1 and len(matrices[0]["fixed"]) == 24
    assert "parent_bindings" not in matrices[0]["context"] and "editorial_parent_witnesses" not in matrices[0]["context"]
    assert case == before and service.model_calls == []


@pytest.mark.parametrize("parent,code", [("unknown", "insufficient_context"), ("violates", "parent_condition_conflict")])
@pytest.mark.asyncio
async def test_exact_verified_finite_witness_cannot_override_real_semantic_parent_rejection(parent, code):
    case, _, _, _ = await finite_child()
    with pytest.raises(_StageConsistencyError, match=code):
        await run_scope(case, parent=parent)


@pytest.mark.asyncio
async def test_nonrecommended_actual_option_parent_conflict_still_blocks_the_whole_candidate():
    case = ordinary_case(5)
    case["result"]["question"]["options"][4]["description"] = "把例子放在定义前面，替换先定义再举例。"
    covered = []
    def reject_last(payload, kwargs, response):
        covered.extend(response["checks"])
        if "q0_o4" in response["checks"]:
            binding = payload["question_contexts"]["q0"]["parent_bindings"]["q0_o4"]
            assert binding["actual_unit"]["candidate_text"].endswith("把例子放在定义前面，替换先定义再举例。")
            response["checks"]["q0_o4"]["parent"] = "violates"
    with pytest.raises(_StageConsistencyError, match="parent_condition_conflict"):
        await run_scope(case, mutate=reject_last)
    assert set(covered) == {"q0_prompt", "q0_space"} | {"q0_o{}".format(index) for index in range(5)}


@pytest.mark.parametrize("tamper", ["fixed", "skill", "packet", "ancestor", "result"])
@pytest.mark.asyncio
async def test_async_change_of_any_current_authority_packet_parent_or_candidate_is_rejected(tamper):
    case, _, _, _ = await finite_child()
    def mutate(payload, kwargs, response):
        if tamper == "fixed":
            case["task"]["fixed_decisions"][-1]["answer"] += "异步改写"
        elif tamper == "skill":
            case["task"]["user_constraints"]["selected_skills"][0]["executionSteps"][0]["objective"] += "异步改写"
        elif tamper == "packet":
            case["task"]["editorial_compilation"]["packet"]["parsed"]["question"]["options"][0]["description"] += "异步改写"
        elif tamper == "ancestor":
            case["task"]["ancestor_conditions"][0]["option_description"] += "异步改写"
        else:
            case["result"]["question"]["options"][1]["description"] += "异步改写"
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await run_scope(case, mutate=mutate)


@pytest.mark.asyncio
async def test_async_local_binding_tamper_is_rejected_even_when_source_authority_is_unchanged(monkeypatch):
    held = []
    original = Coordinator._parent_scope_binding
    def bind(unit, context):
        binding = original(unit, context)
        if binding is not None:
            held.append(binding)
        return binding
    monkeypatch.setattr(Coordinator, "_parent_scope_binding", staticmethod(bind))
    def mutate(payload, kwargs, response):
        for binding in held:
            binding["ancestor_conditions"][0]["option_description"] += "异步伪造"
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await run_scope(ordinary_case(), mutate=mutate)


@pytest.mark.parametrize("changed", ["question", "earlier_parent", "witness_option", "ref"])
@pytest.mark.asyncio
async def test_wrong_actual_context_or_other_variant_witness_cannot_be_used_as_evidence(changed):
    case, _, _, _ = await finite_child()
    units, context = source_context(case)
    schemas = {unit["id"]: Coordinator._bound_scope_schema(unit, context, case["task"], {"pass": ("none", [])}) for unit in units}
    supplied = copy.deepcopy(context)
    supplied["parent_bindings"] = {unit["id"]: Coordinator._parent_scope_binding(unit, context) for unit in units}
    supplied["editorial_parent_witnesses"] = {unit["id"]: Coordinator._editorial_parent_witness(unit=unit,
        context=context, binding=supplied["parent_bindings"][unit["id"]], **{key:case[key] for key in ("task", "result", "topic")}) for unit in units}
    if changed == "question":
        altered = copy.deepcopy(context)
        altered["question"]["options"][0]["description"] = "改写真实方案"
        with pytest.raises(_StageConsistencyError, match="checker_invalid"):
            Coordinator._editorial_parent_witness(unit=units[0], context=altered,
                binding=supplied["parent_bindings"][units[0]["id"]], **{key:case[key] for key in ("task", "result", "topic")})
        return
    if changed == "earlier_parent":
        supplied["parent_bindings"][units[0]["id"]]["ancestor_conditions"][0]["parent_prompt"] += "改写真实父"
    elif changed == "witness_option":
        supplied["editorial_parent_witnesses"]["q0_o0"] = supplied["editorial_parent_witnesses"]["q0_o1"]
    else:
        schemas[units[0]["id"]]["properties"]["parent_evidence_ref"]["enum"] = ["0" * 64]
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        Coordinator._validate_parent_scope_inputs(units=units, source_contexts={"q0":context},
            supplied_contexts={"q0":supplied}, schemas=schemas, **{key:case[key] for key in ("task", "result", "topic")})


@pytest.mark.asyncio
async def test_empty_root_has_no_parent_binding_ref_or_nested_parent_witness():
    _, _, case, _ = await capture_compilation()
    _, _, scopes, _ = await run_scope(case)
    for payload, kwargs in scopes:
        context = payload["question_contexts"]["q0"]
        assert "parent_bindings" not in context and "editorial_parent_witnesses" not in context
        for record in kwargs["json_schema"]["properties"]["checks"]["properties"].values():
            assert record["properties"]["parent"]["enum"] == ["no_parent"]
            assert "parent_evidence_ref" not in record["properties"]


@pytest.mark.asyncio
async def test_ordinary_free_parent_uses_full_actual_conditions_without_program_ir_or_extra_calls():
    case = ordinary_case(5)
    _, service, scopes, matrices = await run_scope(case)
    assert matrices and service.model_calls == []
    for payload, kwargs in scopes:
        context = payload["question_contexts"]["q0"]
        assert "editorial_parent_witnesses" not in context
        assert set(context["parent_bindings"]) == {unit["id"] for unit in payload["validation_task"]["units"]}
        assert "无需父方案已执行" in kwargs["system_prompt"] and "不是正文已经写出" in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_missing_parent_ref_cannot_silently_fall_back_to_the_old_protocol():
    def drop_ref(payload, kwargs, response):
        response["checks"][next(iter(response["checks"]))].pop("parent_evidence_ref")
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await run_scope(ordinary_case(), mutate=drop_ref)


@pytest.mark.asyncio
async def test_source_authority_changed_during_prior_novelty_await_is_rejected_before_scope():
    case = ordinary_case()
    coordinator, service = setup([], check_consistency=True)
    async def novelty(**kwargs):
        case["task"]["user_constraints"]["late_edit"] = "异步添加新用户范围"
        return [proof({"question": kwargs["candidate"]["question"]})]
    coordinator._check_new_decision_space = novelty
    with pytest.raises(_StageConsistencyError, match="checker_invalid"):
        await Coordinator._check_stage_consistency(coordinator, **{**MODEL, **case})
    assert service.model_calls == []
