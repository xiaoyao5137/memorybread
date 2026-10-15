"""Writing choices keep actual operations and selected Skill policies separate."""
import copy
import hashlib
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.test_brainstorm_choice_relationship import complete_action, fixture
from tests.test_creation_brainstorm import StubCreationService


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}
FACTS = ("proposes_nonwriting_operation", "violates_selected_skill_policy")


def selected_profiles():
    return [{"id": "weekly-brief", "title": "项目复盘", "summary": "完整所选政策",
        "skillDescription": {"deliverables": ["结构化书面复盘"], "documentTypes": ["复盘报告"]},
        "executionSteps": [{"id": "step_{}".format(index), "objective": "保留原始取值及原排序"}
            for index in range(15)] + [{"id": "last-policy",
                "objective": "保持原始项目行关联，按成本降序取前十项目，不按领域聚合；只提供书面说明"}]}]


def profile_sha(skills):
    return hashlib.sha256(json.dumps(skills, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def certificate(question):
    return {"unresolved_axis": question["prompt"], "relation": "same_unresolved_axis", "decision_ids": [],
        "options": {option["id"]: {"evidence": option["label"], "action_span": complete_action(option),
            "role": "new_method", "fixed_id": "none"} for option in question["options"]}}


class ScopeService(StubCreationService):
    def __init__(self, mutation=None):
        super().__init__([])
        self.mutation = mutation

    async def _stream_direct_completion(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.model_calls.append(kwargs)
        schema = kwargs["json_schema"]["properties"]["checks"]["properties"]
        checks = {identifier: {field: "no" if prop["enum"] == ["yes", "no", "unknown"] else prop["enum"][0]
            for field, prop in unit["properties"].items()} for identifier, unit in schema.items()}
        if self.mutation:
            self.mutation(checks, json.loads(kwargs["user_prompt"]))
        yield json.dumps({"checks": checks}, ensure_ascii=False)


async def run_scope(*, skills=None, stage="writing", kind="node", records=None,
                    result=None, task=None, mutation=None):
    value, _, default_task = fixture()
    default_task.update(cleared_constraints=[], user_constraints={"root_request": "写项目复盘", "brief_edits": {}})
    if skills is not None:
        default_task["user_constraints"]["selected_skills"] = skills
    task = default_task if task is None else task
    selected = {"id": "topic", "title": "结果说明", "goal": "保持既定数据及方法，仅组织本章说明段"}
    if kind == "review":
        records = records or [{"question": value["question"], "topic": selected, "ancestor_conditions": []}]
        value = {"review_questions": records}
        selected = {"topics": [selected]}
    else:
        value = value if result is None else result
    service = ScopeService(mutation)
    coordinator = Coordinator(service)
    matrices = []
    async def novelty(**kwargs):
        source = kwargs["candidate"]
        questions = [record["question"] for record in source["review_questions"]] if "review_questions" in source else [source["question"]]
        return [certificate(question) for question in questions]
    async def matrix(**kwargs):
        matrices.append(copy.deepcopy(kwargs))
    coordinator._check_new_decision_space = novelty
    coordinator._check_fixed_decision_matrix = matrix
    await coordinator._check_stage_consistency(stage=stage, kind=kind, topic=selected,
        task=task, result=value, other_topics=[], **MODEL)
    return coordinator, service, matrices


@pytest.mark.parametrize("kind", ["node", "review"])
@pytest.mark.asyncio
async def test_every_writing_unit_binds_complete_selected_profiles_with_typed_program_identity(kind):
    skills = selected_profiles()
    before = copy.deepcopy(skills)
    _, service, matrices = await run_scope(skills=skills, kind=kind)
    seen = []
    for call, prompt in zip(service.model_calls, service.prompts):
        payload = json.loads(prompt)
        assert payload["authority"]["user_constraints"]["selected_skills"] == before
        assert payload["authority"]["selected_skill_policy_binding"] == {
            "profiles_path": "authority.user_constraints.selected_skills",
            "profile_count": 1, "profiles_sha256": profile_sha(before)}
        assert type(payload["authority"]["selected_skill_policy_binding"]["profile_count"]) is int
        for identifier, unit in call["json_schema"]["properties"]["checks"]["properties"].items():
            fields = unit["properties"]
            assert fields["selected_skill_policy_ref"]["enum"] == [profile_sha(before)]
            assert fields["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
            assert fields["proposes_nonwriting_operation"]["enum"] == ["yes", "no", "unknown"]
            seen.append(identifier)
        assert call["temperature"] == 0.0 and call["num_predict"] <= 4000
        assert all(context["writing_organization_required"] is True for context in payload["question_contexts"].values())
        assert "不执行Skill文内" in call["system_prompt"]
        assert "不能用24条固定规则兼容或选项新颖性证书替代" in call["system_prompt"]
    assert set(seen) == {"q0_prompt", "q0_space", "q0_o0", "q0_o1"} and len(seen) == 4
    assert matrices[0]["context"]["writing_organization_required"] is True
    assert skills == before


@pytest.mark.parametrize("skills", [None, []])
@pytest.mark.asyncio
async def test_no_selected_skill_has_no_fabricated_policy_ref_or_unknown(skills):
    _, service, _ = await run_scope(skills=skills)
    for call, prompt in zip(service.model_calls, service.prompts):
        assert "selected_skill_policy_binding" not in json.loads(prompt)["authority"]
        for unit in call["json_schema"]["properties"]["checks"]["properties"].values():
            fields = unit["properties"]
            assert "proposes_nonwriting_operation" in fields
            assert "selected_skill_policy_ref" not in fields and "violates_selected_skill_policy" not in fields


@pytest.mark.asyncio
async def test_nonwriting_stage_keeps_operation_absent_and_checks_complete_selected_skill_policy():
    profiles = selected_profiles()
    _, service, matrices = await run_scope(skills=profiles, stage="detail")
    for call, prompt in zip(service.model_calls, service.prompts):
        assert json.loads(prompt)["authority"]["selected_skill_policy_binding"] == {
            "profiles_path": "authority.user_constraints.selected_skills", "profile_count": 1,
            "profiles_sha256": profile_sha(profiles)}
        for unit in call["json_schema"]["properties"]["checks"]["properties"].values():
            fields = unit["properties"]
            assert "proposes_nonwriting_operation" not in fields
            assert fields["selected_skill_policy_ref"]["enum"] == [profile_sha(profiles)]
            assert fields["violates_selected_skill_policy"]["enum"] == ["yes", "no", "unknown"]
    assert "writing_organization_required" not in matrices[0]["context"]


@pytest.mark.parametrize("kind", ["node", "review"])
@pytest.mark.parametrize("field", FACTS)
@pytest.mark.parametrize("value,code", [("yes", "topic_scope"), ("unknown", "insufficient_context")])
@pytest.mark.asyncio
async def test_atomic_operation_and_skill_failures_block_even_after_novelty_pass(kind, field, value, code):
    def mutate(checks, payload):
        if "q0_o1" in checks:
            checks["q0_o1"][field] = value
    with pytest.raises(BrainstormGenerationError, match=code):
        await run_scope(skills=selected_profiles(), kind=kind, mutation=mutate)


@pytest.mark.parametrize("statement,field", [
    ("先按分类重新汇总计算整体成本与收益，再写说明", "proposes_nonwriting_operation"),
    ("新增点击高亮后自动展开项目详情的工具", "proposes_nonwriting_operation"),
    ("将书面复盘转换为交互式演示页面", "proposes_nonwriting_operation"),
    ("按领域聚合原始前十项目，用组内排名替代原成本降序", "violates_selected_skill_policy"),
])
@pytest.mark.asyncio
async def test_nonrecommended_description_cannot_hide_real_operation_or_late_skill_policy_violation(statement, field):
    result, _, _ = fixture()
    option = result["question"]["options"][1]
    option.update(label="组织说明段", description=statement, recommended=False)
    def mutate(checks, payload):
        if "q0_o1" in checks:
            assert checks["q0_o1"]["claim_evidence"] == complete_action(option)
            assert payload["question_contexts"]["q0"]["question"]["options"][1] == option
            assert payload["authority"]["user_constraints"]["selected_skills"][0]["executionSteps"][-1]["id"] == "last-policy"
            checks["q0_o1"][field] = "yes"
    with pytest.raises(BrainstormGenerationError, match="topic_scope"):
        await run_scope(skills=selected_profiles(), result=result, mutation=mutate)


@pytest.mark.parametrize("statement", [
    "仅解说已定计算公式与已有结果，先解释分子再解释分母，不重新计算数据",
    "原成本降序与全部原表行保持不变，仅在表后分组介绍现有结果",
    "用文字说明已有流程，不检索、采集或实施新的工具；不声称数据已经取得",
])
@pytest.mark.asyncio
async def test_explaining_existing_formula_process_or_results_keeps_no_operation_or_policy_violation(statement):
    result, _, _ = fixture()
    result["question"]["options"][1]["description"] = statement
    _, service, matrices = await run_scope(skills=selected_profiles(), result=result)
    assert matrices and all(call["num_predict"] <= 4000 for call in service.model_calls)
    assert any(statement in prompt for prompt in service.prompts)
    assert "仅安排如何解说既定公式" in service.model_calls[0]["system_prompt"]


@pytest.mark.parametrize("mutation", [
    lambda cell: cell.pop("proposes_nonwriting_operation"),
    lambda cell: cell.pop("violates_selected_skill_policy"),
    lambda cell: cell.pop("selected_skill_policy_ref"),
    lambda cell: cell.update(selected_skill_policy_ref="0" * 64),
    lambda cell: cell.update(selected_skill_policy_ref=profile_sha([])),
    lambda cell: cell.update(selected_skill_policy_ref=["not-a-program-ref"]),
    lambda cell: cell.update(violates_selected_skill_policy="pass"),
    lambda cell: cell.update(proposes_nonwriting_operation="no "),
])
@pytest.mark.asyncio
async def test_missing_forged_or_nonfinite_policy_certificate_fails_closed(mutation):
    def mutate(checks, payload):
        if "q0_o1" in checks:
            mutation(checks["q0_o1"])
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_scope(skills=selected_profiles(), mutation=mutate)


@pytest.mark.parametrize("mutation", [
    lambda profiles: profiles[0]["executionSteps"][-1].update(objective="改成按领域聚合"),
    lambda profiles: profiles.append({"id": "new-policy", "executionSteps": []}),
    lambda profiles: profiles[0].update(title="调用期间被替换的完整profile"),
])
@pytest.mark.asyncio
async def test_complete_actual_skill_identity_change_during_scope_call_invalidates_snapshot(mutation):
    skills = selected_profiles()
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_scope(skills=skills, mutation=lambda checks, payload: mutation(skills))


@pytest.mark.parametrize("mutation", [
    lambda binding: binding.update(profile_count=True),
    lambda binding: binding.update(profiles_path="authority.user_constraints.compact_skills"),
    lambda binding: binding.update(profiles_sha256="0" * 64),
    lambda binding: binding.update(extra="not-program-bound"),
])
@pytest.mark.asyncio
async def test_compiled_checker_binding_rejects_wrong_typed_count_path_hash_or_extra_metadata(monkeypatch, mutation):
    real_deepcopy = copy.deepcopy
    def corrupt_binding(value, *args, **kwargs):
        result = real_deepcopy(value, *args, **kwargs)
        if isinstance(result, dict) and set(result) == {"profiles_path", "profile_count", "profiles_sha256"}:
            mutation(result)
        return result
    monkeypatch.setattr("creation.brainstorm_stages.copy.deepcopy", corrupt_binding)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_scope(skills=selected_profiles())


@pytest.mark.asyncio
async def test_model_cannot_fabricate_policy_unknown_when_no_skill_is_selected():
    def mutate(checks, payload):
        if "q0_o1" in checks:
            checks["q0_o1"].update(selected_skill_policy_ref="0" * 64, violates_selected_skill_policy="unknown")
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await run_scope(mutation=mutate)


@pytest.mark.asyncio
async def test_maximum_review_pool_keeps_all_actual_units_and_policy_fields_within_each_budget():
    result, _, _ = fixture()
    records = []
    selected = {"id": "topic", "title": "结果说明", "goal": "保持既定数据及方法，仅组织本章说明段"}
    for index in range(12):
        question = copy.deepcopy(result["question"])
        question["prompt"] = "第{}份记录的说明段如何组织？".format(index + 1)
        question["options"] = [{"id": "option_{}".format(oi), "label": "组织方法{}".format(oi),
            "description": "按既定记录组织说明文字" * 4} for oi in range(5)]
        records.append({"question": question, "topic": selected, "ancestor_conditions": []})
    before = copy.deepcopy(records)
    _, service, matrices = await run_scope(skills=selected_profiles(), kind="review", records=records)
    units = [identifier for call in service.model_calls
        for identifier in call["json_schema"]["properties"]["checks"]["properties"]]
    expected = {"q{}_{}".format(qi, suffix) for qi in range(12)
        for suffix in ["prompt", "space"] + ["o{}".format(oi) for oi in range(5)]}
    assert len(units) == 84 and set(units) == expected and len(set(units)) == len(units)
    assert len(service.model_calls) > 1 and len(matrices) == 12 and records == before
    assert all(1200 <= call["num_predict"] <= 4000 for call in service.model_calls)
