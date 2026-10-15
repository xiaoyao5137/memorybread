"""Reviewed value IDs close free-copy channels without bypassing old gates."""
import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_root_axes import RootAxisCompiler, load_attribute_values, load_root_axes_binding
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.test_brainstorm_atomic_roots import atomic_proof, atomic_request, raw_value_selection
from tests.test_brainstorm_stage_citations import MEMORY, NO_MEMORY
from tests.test_brainstorm_stage_nodes import fixed_decision, setup


MODULE = importlib.import_module("creation.brainstorm_root_axes")
ASSET_ROOT = Path(__file__).resolve().parents[2] / "shared" / "creation-brainstorm"


@pytest.fixture(autouse=True)
def isolate_registry_environment(monkeypatch):
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH", raising=False)


def current_axis(identifier="audience"):
    return next(axis for axis in load_root_axes_binding()["config"]["axes"] if axis["id"] == identifier)


def selections(identifiers, reference="original"):
    return {"options": [{"value_id": identifier, "recommended": index == 0,
        "memory_choice": reference if index == 0 else "original"}
        for index, identifier in enumerate(identifiers)]}


def long_selected_skills():
    first = {"id": "policy", "skillDescription": {"rules": "全部字段、行列关系与时期必须保留" * 1000},
        "executionSteps": [{"id": "rule", "instructions": "已定计算规则不可重选" * 1000}],
        "unknown_policy_tail": "最后一条政策仍须保留"}
    return [first] + [{"id": "policy{}".format(index),
        "skillDescription": {"rules": "第{}项所选Skill原规则须完整保留".format(index)},
        "unknown_policy_tail": "不能裁掉第{}项未知政策字段".format(index)} for index in range(2, 6)]


def installed_registry(monkeypatch, tmp_path, config):
    path = tmp_path / "registry.json"
    raw = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    monkeypatch.setenv("MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH", str(path))
    return {"config_sha256": hashlib.sha256(raw).hexdigest(), "config": copy.deepcopy(config)}


def task_and_result(memory=NO_MEMORY):
    request = atomic_request()
    proof = atomic_proof(request)
    selection = raw_value_selection()
    axis = current_axis()
    result = Coordinator._render_root_value_selection(proof, selection, copy.deepcopy(memory), [], [])
    task = {"program_bound_atomic_root": proof, "current_axis": axis,
        "current_value_capability": RootAxisCompiler(request["root_axes_binding"]).value_capability(axis),
        "node_goal": axis["goal"], "depth": 1, "max_options": 5, "max_next_goals": 0,
        "ancestor_conditions": [], "program_bound_root_values": {"selection": copy.deepcopy(selection),
            "memory": copy.deepcopy(memory), "previous": [], "existing_bodies": []}}
    return request, task, result


def verify(request, task, result):
    Coordinator._verify_atomic_root_compilation(stage="direction", kind="node",
        topic=request["topic"], task=task, result=result)


def test_reviewed_asset_closes_current_registry_and_has_no_provider_owned_copy():
    raw = (ASSET_ROOT / "attribute-values-v1.json").read_bytes()
    capability = load_attribute_values()
    registry = load_root_axes_binding()["config"]
    assert MODULE._value_capabilities_path() == ASSET_ROOT / "attribute-values-v1.json"
    assert hashlib.sha256(raw).hexdigest() == registry["value_capabilities_sha256"] == MODULE.VALUE_CAPABILITIES_SHA256
    assert capability["renderer_id"] == "atomic_values_v1"
    assert len(capability["axes"]) == 11
    assert all(axis["type"] == "multi_choice" and axis["single_choice_reason"] == ""
        for axis in capability["axes"])
    for axis in registry["axes"]:
        known = next(item for item in capability["axes"] if item["axis_id"] == axis["id"])
        assert axis["value_ids"] == [item["id"] for item in known["values"]]


def test_capability_asset_cannot_be_redirected_by_caller_environment(monkeypatch, tmp_path):
    fake = tmp_path / "model-values.json"
    fake.write_text('{"axes":[],"description":"invent a report strategy"}')
    monkeypatch.setenv("MEMORYBREAD_BRAINSTORM_ATTRIBUTE_VALUES_PATH", str(fake))
    assert MODULE._value_capabilities_path() == ASSET_ROOT / "attribute-values-v1.json"
    assert len(load_attribute_values()["axes"]) == 11


def test_frozen_runtime_loads_both_real_assets_from_the_bundled_directory(monkeypatch, tmp_path):
    directory = tmp_path / "creation_brainstorm"
    directory.mkdir()
    for name in ("root-axes-v1.json", "attribute-values-v1.json"):
        (directory / name).write_bytes((ASSET_ROOT / name).read_bytes())
    monkeypatch.setattr(MODULE.sys, "frozen", True, raising=False)
    monkeypatch.setattr(MODULE.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert MODULE._value_capabilities_path() == directory / "attribute-values-v1.json"
    value = load_root_axes_binding()
    assert RootAxisCompiler(value).value_capability(value["config"]["axes"][0])["axis_id"] == "audience"


@pytest.mark.parametrize("change", ["missing", "whitespace_only", "label", "unselected_axis", "invalid_json", "oversized"])
def test_actual_immutable_capability_bytes_reject_even_self_consistent_local_registry_hash(monkeypatch, tmp_path, change):
    path = tmp_path / "values.json"
    raw = (ASSET_ROOT / "attribute-values-v1.json").read_bytes()
    if change == "missing":
        pass
    elif change == "whitespace_only":
        path.write_bytes(raw + b" ")
    elif change == "invalid_json":
        path.write_bytes(b"{")
    elif change == "oversized":
        path.write_bytes(b" " * (MODULE.MAX_REGISTRY_BYTES + 1))
    else:
        value = json.loads(raw)
        axis = value["axes"][0] if change == "label" else value["axes"][-1]
        axis["values"][0]["label"] = "data verification and report already generated"
        path.write_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(MODULE, "_value_capabilities_path", lambda: path)
    with pytest.raises(BrainstormGenerationError):
        load_attribute_values()
    with pytest.raises(BrainstormGenerationError):
        load_root_axes_binding()


@pytest.mark.parametrize("change", ["hash", "missing_hash", "old_version", "reversed", "unknown", "wrong_axis", "duplicate", "one", "empty", "alias", "selector_extra"])
def test_registry_cannot_replace_reviewed_asset_or_widen_axis_value_selectors(monkeypatch, tmp_path, change):
    config = copy.deepcopy(load_root_axes_binding()["config"])
    axis = config["axes"][0]
    if change == "hash":
        config["value_capabilities_sha256"] = "0" * 64
    elif change == "missing_hash":
        config.pop("value_capabilities_sha256")
    elif change == "old_version":
        config["registry_version"] = 3
    elif change == "reversed":
        axis["value_ids"] = list(reversed(axis["value_ids"]))
    elif change == "unknown":
        axis["value_ids"][0] = "audience.model_generated"
    elif change == "wrong_axis":
        axis["value_ids"][0] = "purpose.understand_state"
    elif change == "duplicate":
        axis["value_ids"][1] = axis["value_ids"][0]
    elif change == "one":
        axis["value_ids"] = axis["value_ids"][:1]
    elif change == "empty":
        axis["value_ids"] = []
    elif change == "alias":
        axis["value_ids"][0] = "执行参与者"
    else:
        axis["value_labels"] = ["invented free labels"]
    supplied = installed_registry(monkeypatch, tmp_path, config)
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(supplied)


def test_configurable_subset_is_a_new_registry_not_a_legacy_snapshot(monkeypatch, tmp_path):
    config = copy.deepcopy(load_root_axes_binding()["config"])
    config["registry_version"] = 5
    config["axes"] = [config["axes"][0]]
    config["axes"][0]["value_ids"] = ["audience.participants", "audience.specialists", "audience.general_readers"]
    supplied = installed_registry(monkeypatch, tmp_path, config)
    compiler = RootAxisCompiler(supplied)
    known = compiler.value_capability(supplied["config"]["axes"][0])
    assert [item["id"] for item in known["values"]] == config["axes"][0]["value_ids"]
    selected = selections(["audience.general_readers", "audience.participants"])
    result = compiler.compile_values(selected, supplied["config"]["axes"][0], 2, [])
    assert [item["label"] for item in result["question"]["options"]] == ["非专业读者", "执行参与者"]
    with pytest.raises(BrainstormGenerationError):
        compiler.compile_values(raw_value_selection(), supplied["config"]["axes"][0], 2, [])


@pytest.mark.parametrize("axis_id", ["audience", "purpose", "content_focus", "information_granularity",
    "concept_explanation_depth", "evidence_presentation", "narrative_perspective", "structural_order",
    "rhythm", "tone", "action_plan"])
def test_every_axis_renders_only_selected_known_values_in_provider_order(axis_id):
    compiler = RootAxisCompiler(load_root_axes_binding())
    axis = current_axis(axis_id)
    known = compiler.value_capability(axis)
    selected = selections([item["id"] for item in reversed(known["values"][:2])])
    before = copy.deepcopy((axis, selected))
    compiled = compiler.compile_values(selected, axis, 5, [])
    assert compiled["next_goals"] == []
    assert compiled["question"]["type"] == "multi_choice"
    assert compiled["question"]["prompt_body"] == known["prompt_body"]
    assert [(item["label"], item["description"]) for item in compiled["question"]["options"]] == [
        (item["label"], item["description"]) for item in reversed(known["values"][:2])]
    assert (axis, selected) == before


@pytest.mark.parametrize("change", ["label", "description", "why_now", "prompt", "type", "children", "free_question",
    "mixed", "unknown", "wrong_axis", "alias", "padded_id", "bool_id", "duplicate", "missing_id", "empty",
    "one", "over_budget", "not_list", "recommended_int", "recommended_false_int", "recommended_string",
    "none_recommended", "two_recommended", "unknown_source", "source_not_string", "option_not_object"])
def test_invalid_wire_never_becomes_a_valid_question_by_dropping_bad_fields(change):
    compiler = RootAxisCompiler(load_root_axes_binding())
    raw = raw_value_selection()
    first = raw["options"][0]
    budget = 5
    if change in {"label", "description"}:
        first[change] = "已确认缩容，重点展示年化节省"
    elif change in {"why_now", "prompt", "type", "children"}:
        raw[change] = "current report is already generated"
    elif change == "free_question":
        raw = {"question": {"prompt_body": "arbitrary report strategy"}}
    elif change == "mixed":
        raw["question"] = {"prompt_body": "arbitrary report strategy"}
    elif change in {"unknown", "wrong_axis", "alias", "padded_id", "bool_id"}:
        first["value_id"] = {"unknown": "audience.mixed_all", "wrong_axis": "purpose.form_judgement",
            "alias": "执行参与者", "padded_id": "audience.participants ", "bool_id": True}[change]
    elif change == "duplicate":
        raw["options"][1]["value_id"] = first["value_id"]
    elif change == "missing_id":
        first.pop("value_id")
    elif change == "empty":
        raw["options"] = []
    elif change == "one":
        raw["options"] = raw["options"][:1]
    elif change == "over_budget":
        raw["options"].append({"value_id": "audience.specialists", "recommended": False, "memory_choice": "original"})
        budget = 2
    elif change == "not_list":
        raw["options"] = tuple(raw["options"])
    elif change.startswith("recommended_"):
        first["recommended"] = {"recommended_int": 1, "recommended_false_int": 0, "recommended_string": "true"}[change]
    elif change == "none_recommended":
        first["recommended"] = False
    elif change == "two_recommended":
        raw["options"][1]["recommended"] = True
    elif change == "unknown_source":
        first["memory_choice"] = "reference:historical_report_not_retrieved"
    elif change == "source_not_string":
        first["memory_choice"] = False
    else:
        raw["options"][0] = None
    before = copy.deepcopy(raw)
    with pytest.raises(BrainstormGenerationError):
        compiler.compile_values(raw, current_axis(), budget, [])
    assert raw == before


@pytest.mark.asyncio
async def test_actual_prepare_uses_finite_schema_and_cannot_repair_program_owned_templates():
    c, service = setup([raw_value_selection()])
    async def forbidden(*args, **kwargs):
        pytest.fail("finite program-owned question must not be sent to free-copy repair")
    c._repair_question_copy = forbidden
    result = await c.stage_batch(**atomic_request())
    assert result["root_value_selection"] == raw_value_selection()
    assert result["question"]["prompt"] == "本次创作面向哪些阅读对象？"
    assert result["question"]["why_now"] == "确定阅读对象范围，供后续创作沿用。"
    assert [option["label"] for option in result["question"]["options"]] == ["执行参与者", "管理决策者"]
    assert result["question"]["options"][0]["description"] == "承担相关执行工作、直接参与事情推进的读者。"
    schema = service.model_calls[0]["json_schema"]
    assert set(schema["properties"]) == {"options"}
    assert set(schema["properties"]["options"]["items"]["properties"]) == {"value_id", "recommended", "memory_choice"}
    assert "purpose.form_judgement" not in schema["properties"]["options"]["items"]["properties"]["value_id"]["enum"]
    context = json.loads(service.prompts[0])
    assert "current_value_capability" in context["generation_task"]
    assert "program_bound_root_values" not in context["generation_task"]
    assert "仅返回{options:" in service.model_calls[0]["system_prompt"]
    assert len(service.model_calls) == 1


@pytest.mark.parametrize("field", ["label", "description", "type", "next_goals"])
@pytest.mark.asyncio
async def test_prepare_rejects_injected_value_copy_without_calling_semantic_acceptance(field):
    raw = raw_value_selection()
    raw["options"][0][field] = "已确认缩容，生成审校方案"
    c, service = setup([raw, raw])
    async def forbidden(**kwargs):
        pytest.fail("invalid primitive wire must not reach semantic acceptance")
    c._check_stage_consistency = forbidden
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**atomic_request())
    assert len(service.model_calls) == Coordinator.MAX_NODE_GENERATION_ATTEMPTS


@pytest.mark.parametrize("change", ["display_label", "display_description", "prompt", "why_now", "answer_template",
    "question_type", "display_recommended_int", "selection_recommended_int", "selection_source", "selection_order",
    "selection_extra", "option_id", "children", "capability_text", "prior_prompt", "existing_body"])
def test_post_check_recompilation_binds_every_field_and_exact_primitives(change):
    request, task, result = task_and_result()
    verify(request, task, result)
    option = result["question"]["options"][0]
    if change == "display_label":
        option["label"] = "技术团队且收益优先"
    elif change == "display_description":
        option["description"] += "，仅保留汇总"
    elif change in {"prompt", "why_now", "answer_template"}:
        result["question"][change] += "已完成当前周报"
    elif change == "question_type":
        result["question"]["type"] = "single_choice"
    elif change == "display_recommended_int":
        option["recommended"] = 1
    elif change == "selection_recommended_int":
        result["root_value_selection"]["options"][0]["recommended"] = 1
    elif change == "selection_source":
        result["root_value_selection"]["options"][0]["memory_choice"] = "reference:m1"
    elif change == "selection_order":
        result["root_value_selection"]["options"].reverse()
    elif change == "selection_extra":
        result["root_value_selection"]["options"][0]["label"] = "model text"
    elif change == "option_id":
        option["id"] = "c"
    elif change == "children":
        result["next_goals"] = [{"option_id": "a", "goals": ["请选择内容侧重"]}]
    elif change == "capability_text":
        task["current_value_capability"]["values"][0]["description"] += "，伪造策略"
    elif change == "prior_prompt":
        task["program_bound_root_values"]["previous"].append(result["question"]["prompt"])
    else:
        task["program_bound_root_values"]["existing_bodies"].append(result["question"]["prompt"])
    with pytest.raises(BrainstormGenerationError):
        verify(request, task, result)


@pytest.mark.parametrize("change", ["selection", "both_selection_and_result", "current_capability", "memory", "display_type"])
@pytest.mark.asyncio
async def test_outer_await_freezes_selection_proof_and_all_actual_sources(change):
    raw = selections(["audience.participants", "audience.decision_makers"], "reference:m1+m2")
    c, service = setup([raw, raw], MEMORY)
    async def mutate(**kwargs):
        values = kwargs["task"]["program_bound_root_values"]
        if change in {"selection", "both_selection_and_result"}:
            values["selection"]["options"][0]["value_id"] = "audience.collaborators"
            if change == "both_selection_and_result":
                kwargs["result"].clear()
                kwargs["result"].update(Coordinator._render_root_value_selection(
                    kwargs["task"]["program_bound_atomic_root"], values["selection"],
                    values["memory"], values["previous"], values["existing_bodies"]))
        elif change == "current_capability":
            kwargs["task"]["current_value_capability"]["type"] = "single_choice"
        elif change == "memory":
            values["memory"]["sources"][-1]["content"] += "unselected source changed"
        else:
            kwargs["result"]["question"]["options"][0]["recommended"] = 1
    c._check_stage_consistency = mutate
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**atomic_request())
    assert len(service.model_calls) == Coordinator.MAX_NODE_GENERATION_ATTEMPTS


@pytest.mark.parametrize("when", ["generation", "checker"])
@pytest.mark.asyncio
async def test_capability_byte_mutation_during_await_cannot_change_a_live_renderer(monkeypatch, tmp_path, when):
    path = tmp_path / "values.json"
    original = (ASSET_ROOT / "attribute-values-v1.json").read_bytes()
    path.write_bytes(original)
    monkeypatch.setattr(MODULE, "_value_capabilities_path", lambda: path)
    c, service = setup([raw_value_selection()])
    if when == "generation":
        original_complete = c._complete
        async def complete(*args, **kwargs):
            output = await original_complete(*args, **kwargs)
            path.write_bytes(original + b" ")
            return output
        c._complete = complete
    else:
        async def check(**kwargs):
            path.write_bytes(original + b" ")
        c._check_stage_consistency = check
    request = atomic_request()
    before = copy.deepcopy(request)
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**request)
    assert request == before and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_full_authority_and_actual_rag_survive_finite_value_selection():
    request = atomic_request()
    root = "完整需求" * 900
    skills = long_selected_skills()
    request.update(root_request=root, selected_skills=skills, decisions=[fixed_decision()])
    raw = selections(["audience.participants", "audience.decision_makers"], "reference:m1+m2")
    c, service = setup([raw], MEMORY)
    calls, checks = [], []
    c._retrieve_memory = lambda *args: calls.append(args) or copy.deepcopy(MEMORY)
    async def inspect(**kwargs):
        task = kwargs["task"]
        assert task["user_constraints"]["root_request"] == root
        assert task["user_constraints"]["selected_skills"] == skills
        assert task["fixed_decisions"][0]["answer"] == request["decisions"][0]["answer"]
        assert task["program_bound_root_values"]["memory"] == MEMORY
        assert len(task["program_bound_root_values"]["memory"]["sources"]) == 6
        checks.append(copy.deepcopy(kwargs))
    c._check_stage_consistency = inspect
    result = await c.stage_batch(**request)
    assert len(calls) == len(checks) == len(service.model_calls) == 1
    context = json.loads(service.prompts[0])
    assert context["root_request"] == root and context["memory"] == MEMORY
    assert context["selected_skills"] == skills
    assert context["selected_skills"][0]["skillDescription"]["rules"].endswith("全部字段、行列关系与时期必须保留")
    assert context["selected_skills"][0]["unknown_policy_tail"] == skills[0]["unknown_policy_tail"]
    assert context["selected_skills"][4]["unknown_policy_tail"] == skills[4]["unknown_policy_tail"]
    referenced = result["question"]["options"][0]
    assert all(item["content"] in referenced["details"] for item in MEMORY["sources"][:2])
    assert referenced["description"] == "承担相关执行工作、直接参与事情推进的读者。"
    assert result["root_value_selection"] == raw


@pytest.mark.asyncio
async def test_registered_plan_generation_keeps_all_full_skills_without_changing_planning_recall_policy():
    skills = long_selected_skills()
    root = "完整规划需求" * 700
    binding = load_root_axes_binding()
    raw = {"axis_ids": ["audience", "purpose"]}
    c, service = setup([raw])
    c._retrieve_memory = lambda *args: pytest.fail("plan retains its original zero-recall contract")
    seen = []
    async def inspect(**kwargs):
        assert kwargs["task"]["user_constraints"]["selected_skills"] == skills
        assert kwargs["task"]["user_constraints"]["root_request"] == root
        assert [topic["root_axis_ids"] for topic in kwargs["result"]["topics"]] == [["audience"], ["purpose"]]
        seen.append(copy.deepcopy(kwargs))
    c._check_plan_consistency = inspect
    result = await c.stage_batch(stage="direction", kind="plan", root_request=root, selected_skills=skills,
        root_axes_binding=binding, context_id="a" * 64, limits={"max_topics": 2}, decisions=[fixed_decision()])
    assert len(service.model_calls) == len(seen) == 1
    context = json.loads(service.prompts[0])
    assert context["root_request"] == root
    assert context["selected_skills"] == skills
    assert context["selected_skills"][0]["skillDescription"]["rules"] == skills[0]["skillDescription"]["rules"]
    assert context["selected_skills"][0]["executionSteps"] == skills[0]["executionSteps"]
    assert context["selected_skills"][4]["unknown_policy_tail"] == skills[4]["unknown_policy_tail"]
    assert context["memory"] == {"status": "skipped", "sources": []}
    assert [topic["root_axis_ids"] for topic in result["topics"]] == [["audience"], ["purpose"]]


@pytest.mark.parametrize("kind", ["plan", "node"])
@pytest.mark.asyncio
async def test_full_skill_context_over_local_budget_rejects_before_provider_without_dropping_policy(monkeypatch, kind):
    monkeypatch.setenv("MEMORYBREAD_CREATION_CONTEXT_TOKENS", "8192")
    c, service = setup([])
    skills = long_selected_skills()
    if kind == "node":
        request = atomic_request()
    else:
        request = {"stage": "direction", "kind": "plan", "root_request": "完整原需求",
            "root_axes_binding": load_root_axes_binding(), "context_id": "a" * 64,
            "limits": {"max_topics": 2}, "decisions": []}
    request["selected_skills"] = skills
    before = copy.deepcopy(request)
    seen = []
    original_guard = c._guard_root_generation_context
    def inspect_guard(prompt, system, output_tokens, model):
        actual = json.loads(prompt)
        assert actual["selected_skills"] == skills
        assert actual["selected_skills"][4]["unknown_policy_tail"] == skills[4]["unknown_policy_tail"]
        seen.append(actual)
        original_guard(prompt, system, output_tokens, model)
    c._guard_root_generation_context = inspect_guard
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**request)
    assert seen and service.model_calls == [] and request == before
