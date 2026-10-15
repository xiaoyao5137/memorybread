"""Finite root attributes preserve scope gates and never accept model-owned goals."""
import asyncio
import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_root_axes import RootAxisCompiler, load_root_axes_binding
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_citations import NO_MEMORY
from tests.test_creation_brainstorm import StubCreationService
from tests.brainstorm_axis_fixtures import axis_plan_manifest, axis_plan_raw

ROOT_IDS = ("audience", "purpose", "content_focus", "information_granularity",
            "concept_explanation_depth", "evidence_presentation", "narrative_perspective",
            "structural_order", "rhythm", "tone", "action_plan")
MODULE = importlib.import_module("creation.brainstorm_root_axes")
STAGES = importlib.import_module("creation.brainstorm_stages")


@pytest.fixture(autouse=True)
def isolate_registry_environment(monkeypatch):
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH", raising=False)


def binding():
    return copy.deepcopy(load_root_axes_binding())


def goals(value, identifiers):
    axes = {axis["id"]: axis for axis in value["config"]["axes"]}
    return {"root_goals": [axes[identifier]["goal"] for identifier in identifiers]}


def install_registry(monkeypatch, tmp_path, value):
    path = tmp_path / "root-axes-v1.json"
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(MODULE, "_registry_path", lambda: path)
    return path, {"config_sha256": hashlib.sha256(raw).hexdigest(), "config": copy.deepcopy(value)}


def test_development_registry_and_binding_cover_exact_implemented_properties():
    value = binding()
    path = MODULE._registry_path()
    assert path == Path(__file__).resolve().parents[2] / "shared/creation-brainstorm/root-axes-v1.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == value["config_sha256"]
    assert json.loads(path.read_bytes()) == value["config"]
    assert tuple(axis["id"] for axis in value["config"]["axes"]) == ROOT_IDS
    assert all(axis["property"] == axis["id"] for axis in value["config"]["axes"])
    assert len({axis["goal"] for axis in value["config"]["axes"]}) == len(ROOT_IDS)
    assert RootAxisCompiler(value).eligible_axes("direction")
    assert RootAxisCompiler(value).eligible_axes("detail")


def test_audience_and_information_depth_keep_both_whole_goals_in_selection_order():
    value = binding()
    raw = {"root_axis_ids": ["information_granularity", "audience"]}
    frozen = copy.deepcopy((value, raw))
    compiler = RootAxisCompiler(value)
    compiled = compiler.compile(raw, "detail", 2)
    assert compiled == goals(value, raw["root_axis_ids"])
    assert len(compiled["root_goals"]) == 2
    compiler.verify(compiled, raw, "detail", 2)
    assert (value, raw) == frozen


@pytest.mark.parametrize("stage,identifiers", [
    ("direction", ["purpose", "audience"]),
    ("detail", ["concept_explanation_depth", "information_granularity"]),
])
def test_two_supported_independent_properties_keep_both_whole_goals_in_selection_order(stage, identifiers):
    value = binding()
    raw = {"root_axis_ids": identifiers}
    compiler = RootAxisCompiler(value)
    result = compiler.compile(raw, stage, 2)
    assert result == goals(value, identifiers) and len(result["root_goals"]) == 2
    compiler.verify(result, raw, stage, 2)


@pytest.mark.parametrize("raw", [
    {"root_axis_ids": ["audience", "audience"]},
    {"root_axis_ids": ["unknown_attribute"]},
    {"root_axis_ids": ["受众范围"]},
    {"root_axis_ids": ["audience"], "root_goals": ["伪造待决定目标"]},
    {"root_goals": ["选择受众范围"]},
    {"root_axis_ids": []},
    {"root_axis_ids": "audience"},
    {"root_axis_ids": [True]},
    {"root_axis_ids": [1]},
    {"root_axis_ids": ["audience " ]},
    {"root_axis_ids": ["audience"], "extra": True},
])
def test_duplicate_unknown_alias_or_mixed_provider_protocol_rejects_whole_plan(raw):
    compiler = RootAxisCompiler(binding())
    before = copy.deepcopy(raw)
    with pytest.raises(BrainstormGenerationError):
        compiler.compile(raw, "direction", 2)
    assert raw == before


def test_root_budget_never_slices_a_complete_registered_selection():
    compiler = RootAxisCompiler(binding())
    raw = {"root_axis_ids": ["information_granularity", "evidence_presentation"]}
    with pytest.raises(BrainstormGenerationError):
        compiler.compile(raw, "detail", 1)
    assert raw["root_axis_ids"] == ["information_granularity", "evidence_presentation"]


@pytest.mark.parametrize("limit", [0, -1, True, "2", None])
def test_root_budget_requires_positive_real_integer(limit):
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(binding()).compile({"root_axis_ids": ["audience"]}, "direction", limit)


@pytest.mark.parametrize("change", ["hash", "snapshot", "extra"])
def test_caller_binding_must_match_full_actual_registry_bytes(change):
    value = binding()
    if change == "hash":
        value["config_sha256"] = "0" * 64
    elif change == "snapshot":
        value["config"]["axes"][0]["goal"] = "模型自定义目标"
    else:
        value["extra"] = "not authority"
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(value)


@pytest.mark.parametrize("field,value", [
    ("id", "audience_range"), ("property", "information_granularity"),
    ("goal", "自行改写的受众目标"), ("definition", "将时间周期也合并为受众"),
    ("title", "自由命名的其它属性"),
    ("decision_mode", "model_terminal"),
    ("stages", ["writing"]),
])
def test_local_config_cannot_define_unimplemented_or_aliased_property(monkeypatch, tmp_path, field, value):
    config = binding()["config"]
    config["axes"][0][field] = value
    _, supplied = install_registry(monkeypatch, tmp_path, config)
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(supplied)


@pytest.mark.parametrize("duplicate", ["canonical", "alias"])
def test_registry_cannot_repeat_a_property_under_same_or_alias_identity(monkeypatch, tmp_path, duplicate):
    config = binding()["config"]
    second = copy.deepcopy(config["axes"][0])
    if duplicate == "alias":
        second["id"] = "audience_range"
    config["axes"].append(second)
    _, supplied = install_registry(monkeypatch, tmp_path, config)
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(supplied)


def test_no_eligible_stage_axis_is_rejected_without_fabricated_root(monkeypatch, tmp_path):
    config = binding()["config"]
    config["axes"] = [axis for axis in config["axes"] if "direction" in axis["stages"]]
    for axis in config["axes"]:
        axis["stages"] = ["direction"]
    _, supplied = install_registry(monkeypatch, tmp_path, config)
    compiler = RootAxisCompiler(supplied)
    assert compiler.eligible_axes("detail") == []
    with pytest.raises(BrainstormGenerationError):
        compiler.compile({"root_axis_ids": ["audience"]}, "detail", 1)


@pytest.mark.parametrize("change", ["goal", "order", "extra"])
def test_semantic_gate_cannot_rewrite_the_program_mapping(change):
    value = binding()
    compiler = RootAxisCompiler(value)
    raw = {"root_axis_ids": ["audience", "purpose"]}
    result = compiler.compile(raw, "direction", 2)
    if change == "goal":
        result["root_goals"][0] = "自由文本替换注册目标"
    elif change == "order":
        result["root_goals"].reverse()
    else:
        result["root_axis_ids"] = raw["root_axis_ids"]
    with pytest.raises(BrainstormGenerationError):
        compiler.verify(result, raw, "direction", 2)


def test_post_check_verification_reloads_actual_bytes_and_detects_drift(monkeypatch, tmp_path):
    config = binding()["config"]
    path, supplied = install_registry(monkeypatch, tmp_path, config)
    compiler = RootAxisCompiler(supplied)
    raw = {"root_axis_ids": ["audience"]}
    result = compiler.compile(raw, "direction", 1)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(BrainstormGenerationError):
        compiler.verify(result, raw, "direction", 1)


def test_frozen_loader_uses_bundled_asset_and_missing_asset_does_not_fallback(monkeypatch, tmp_path):
    original = MODULE._registry_path().read_bytes()
    capability = MODULE._value_capabilities_path().read_bytes()
    bundled = tmp_path / "creation_brainstorm/root-axes-v1.json"
    bundled.parent.mkdir()
    bundled.write_bytes(original)
    (bundled.parent / "attribute-values-v1.json").write_bytes(capability)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert MODULE._registry_path() == bundled
    assert load_root_axes_binding()["config_sha256"] == hashlib.sha256(original).hexdigest()
    bundled.unlink()
    with pytest.raises(BrainstormGenerationError):
        load_root_axes_binding()


def test_pyinstaller_includes_the_registry_directory():
    script = Path(__file__).resolve().parents[2] / "scripts/build-macos.sh"
    assert '$PROJECT_ROOT/shared/creation-brainstorm:creation_brainstorm' in script.read_text()


def root_service(raw):
    service = StubCreationService([json.dumps(raw, ensure_ascii=False)] * 2)
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(NO_MEMORY)
    return coordinator, service


async def request_roots(coordinator, **extra):
    budget = BrainstormStageCoordinator.limits(extra.pop("limits", {"max_topics": 1}))
    manifest = axis_plan_manifest("direction", ["audience", "purpose"], budget)
    return await coordinator.stage_batch(stage="direction", kind="topic_plan",
        root_request=extra.pop("root_request", "只使用当前输入规划周报受众与表达目的"),
        decisions=extra.pop("decisions", []), topic=extra.pop("topic", manifest["topics"][0]),
        plan=extra.pop("plan", {"topics": manifest["topics"]}), limits=budget,
        root_limit=2, context_id="a" * 64, **extra)


@pytest.mark.asyncio
async def test_registered_roots_still_run_full_root_skill_and_topic_authority_gate():
    raw = {"root_axis_ids": ["audience", "purpose"]}
    value = binding()
    coordinator, service = root_service(raw)
    root = "只用当前输入，不检索私有材料。" + "原始完整约束" * 300
    skills = [{"id": "policy", "skillDescription": {"rules": ["必需原列全部保留" * 1000]},
               "tail_policy": "不可删除末尾来源与时期门禁"}]
    before = copy.deepcopy(skills)
    seen = []
    async def inspect(**kwargs):
        task = kwargs["task"]
        assert task["user_constraints"]["root_request"] == root
        assert task["user_constraints"]["selected_skills"] == before
        assert task["program_bound_root_axes"]
        assert kwargs["topic"] == axis_plan_manifest("direction", raw["root_axis_ids"], {"max_topics": 1})["topics"][0]
        assert kwargs["result"] == goals(value, raw["root_axis_ids"])
        seen.append(copy.deepcopy(kwargs["result"]))
    coordinator._check_stage_consistency = inspect
    result = await request_roots(coordinator, root_axes_binding=value, root_request=root, selected_skills=skills)
    assert result == goals(value, raw["root_axis_ids"]) and len(seen) == 1
    assert skills == before
    assert service.model_calls == [] and service.prompts == []


@pytest.mark.asyncio
async def test_existing_scope_rejection_is_not_bypassed_by_compiled_roots():
    coordinator, service = root_service({"root_axis_ids": ["audience"]})
    seen = []
    async def reject(**kwargs):
        seen.append(copy.deepcopy(kwargs))
        raise BrainstormGenerationError("仍由完整主题与Skill检查拒绝越界")
    coordinator._check_stage_consistency = reject
    with pytest.raises(BrainstormGenerationError):
        await request_roots(coordinator, root_axes_binding=binding())
    assert len(seen) == 1 and service.model_calls == []


@pytest.mark.asyncio
async def test_free_root_plan_requires_binding_before_any_provider_call():
    coordinator, service = root_service({"root_axis_ids": ["audience"]})
    with pytest.raises(BrainstormGenerationError):
        await request_roots(coordinator)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_writing_and_installed_catalog_roots_do_not_enter_free_registry(monkeypatch, tmp_path):
    from tests.test_brainstorm_stage_nodes import prepare, setup
    from tests.test_brainstorm_catalog import directory, install, request
    def forbidden(*args, **kwargs):
        raise AssertionError("program-bound writing/catalog must bypass free root compiler")
    monkeypatch.setattr(STAGES, "RootAxisCompiler", forbidden)
    coordinator, service = setup([])
    writing = await prepare(coordinator, stage="writing", kind="topic_plan", node=None, root_limit=2)
    assert writing == {"root_goals": [BrainstormStageCoordinator.WRITING_AXES[0]["levels"][0]]}
    assert service.model_calls == []
    catalog, catalog_service, bound, skill = install(tmp_path, directory(stages=("direction",)))
    plan = await request(catalog, bound, skill, stage="direction")
    roots = await request(catalog, bound, skill, stage="direction", kind="topic_plan", topic=plan["topics"][0], root_limit=2)
    assert roots == {"root_goals": ["catalog_slot:root"]}
    assert catalog_service.model_calls == []


def test_registry_subset_and_order_are_capability_configuration_not_required_selection(monkeypatch, tmp_path):
    config = binding()["config"]
    axes = {axis["id"]: axis for axis in config["axes"]}
    config["axes"] = [axes["purpose"], axes["audience"]]
    _, value = install_registry(monkeypatch, tmp_path, config)
    compiler = RootAxisCompiler(value)
    assert [axis["id"] for axis in compiler.eligible_axes("direction")] == ["purpose", "audience"]
    result = compiler.compile({"root_axis_ids": ["audience"]}, "direction", 4)
    assert result == goals(value, ["audience"])
    assert len(result["root_goals"]) == 1


@pytest.mark.parametrize("change", ["schema_version_bool", "registry_kind", "stage_duplicate", "extra_axis_field"])
def test_schema_identity_and_record_shapes_are_strict(monkeypatch, tmp_path, change):
    config = binding()["config"]
    if change == "schema_version_bool":
        config["schema_version"] = True
    elif change == "registry_kind":
        config["kind"] = "custom.arbitrary-goals"
    elif change == "stage_duplicate":
        config["axes"][0]["stages"] = ["direction", "direction"]
    else:
        config["axes"][0]["aliases"] = ["audience_range"]
    _, value = install_registry(monkeypatch, tmp_path, config)
    with pytest.raises(BrainstormGenerationError):
        RootAxisCompiler(value)


@pytest.mark.parametrize("raw", [b'{"axes": [], "axes": []}', b'\xff\xfe', b' '])
def test_registry_parser_rejects_duplicate_keys_and_invalid_text(monkeypatch, tmp_path, raw):
    path = tmp_path / "broken-registry.json"
    path.write_bytes(raw)
    monkeypatch.setattr(MODULE, "_registry_path", lambda: path)
    with pytest.raises(BrainstormGenerationError):
        load_root_axes_binding()


def test_registry_read_is_bounded_before_json_parsing(monkeypatch, tmp_path):
    path = tmp_path / "oversized-registry.json"
    path.write_bytes(b" " * (MODULE.MAX_REGISTRY_BYTES + 1))
    monkeypatch.setattr(MODULE, "_registry_path", lambda: path)
    with pytest.raises(BrainstormGenerationError):
        load_root_axes_binding()


@pytest.mark.asyncio
async def test_plan_capabilities_are_background_and_do_not_fill_topic_or_root_budgets():
    from tests.test_brainstorm_stages import plan
    raw = axis_plan_raw("direction")
    proposed = axis_plan_manifest("direction")
    service = StubCreationService([json.dumps(raw, ensure_ascii=False)])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: (_ for _ in ()).throw(AssertionError("plan must not recall"))
    seen = []
    root = "仅当前资料，保留全部原字段与原规则"
    skills = [{"id": "fixed", "skillDescription": {"rules": ["完整门禁"]}}]
    async def inspect(**kwargs):
        assert kwargs["task"]["user_constraints"]["root_request"] == root
        assert kwargs["task"]["user_constraints"]["selected_skills"] == skills
        seen.append(kwargs["result"])
    coordinator._check_plan_consistency = inspect
    result = await coordinator.stage_batch(stage="direction", kind="plan", root_request=root,
        decisions=[], selected_skills=skills, root_axes_binding=binding())
    assert result == proposed and len(result["topics"]) == 1 and len(seen) == 1
    assert len(service.model_calls) == 1
    context = json.loads(service.prompts[0])
    capabilities = context["root_axis_capabilities"]
    assert capabilities["config_sha256"] == binding()["config_sha256"]
    assert capabilities["axes"] == RootAxisCompiler(binding()).eligible_axes("direction")
    assert "背景" in capabilities["role"] and "不是用户答案" in capabilities["role"]
    assert not {"root_axis_ids", "selected_axis_ids", "root_goals"}.intersection(context)
    assert not {"root_axis_ids", "root_goals"}.intersection(result)
    assert set(service.model_calls[0]["json_schema"]["properties"]) == {"axis_ids"}


@pytest.mark.asyncio
async def test_plan_does_not_ignore_an_explicit_bad_capability_binding():
    coordinator, service = root_service({})
    supplied = binding()
    supplied["config_sha256"] = "0" * 64
    with pytest.raises(BrainstormGenerationError):
        await coordinator.stage_batch(stage="direction", kind="plan", root_request="仅当前资料",
            decisions=[], root_axes_binding=supplied)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_endpoint_keeps_full_core_binding_and_public_string_shape(monkeypatch):
    from tests.test_creation_queue import BrainstormDeadlineThreadQueue
    app = importlib.import_module("creation.app")
    value = binding()
    public = goals(value, ["audience", "purpose"])
    captured = []
    async def stage_batch(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        return copy.deepcopy(public)
    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    payload = {"root_request": "保留原列，选择受众和表达目的", "stage": "direction", "kind": "topic_plan",
        "topic": axis_plan_manifest("direction", ["audience", "purpose"], {"max_topics": 1})["topics"][0],
        "plan": {"topics": axis_plan_manifest("direction", ["audience", "purpose"], {"max_topics": 1})["topics"]},
        "limits": {"max_topics": 1},
        "root_limit": 2, "context_id": "a" * 64, "root_axes_binding": value,
        "selected_skills": [{"id": "policy", "skillDescription": {"rules": ["全部门禁原文"]}}]}
    before = copy.deepcopy(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json=payload)
    assert response.status_code == 200 and response.json() == public
    assert len(captured) == 1
    assert captured[0]["root_axes_binding"] == value
    assert captured[0]["root_request"] == before["root_request"]
    assert captured[0]["selected_skills"] == before["selected_skills"]
    assert captured[0]["topic"] == before["topic"]
    assert payload == before
    await asyncio.to_thread(queue.worker.join, 1)
    assert not queue.worker.is_alive()


def test_source_support_rejects_cross_stage_property_without_relabeling():
    compiler = RootAxisCompiler(binding())
    for stage, identifier in (("direction", "information_granularity"), ("direction", "concept_explanation_depth")):
        with pytest.raises(BrainstormGenerationError):
            compiler.compile({"root_axis_ids": [identifier]}, stage, 1)


def test_explicit_registry_path_is_shared_runtime_configuration(monkeypatch, tmp_path):
    original = MODULE._registry_path().read_bytes()
    path = tmp_path / "configured-root-axes.json"
    path.write_bytes(original)
    monkeypatch.setenv("MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH", str(path))
    assert MODULE._registry_path() == path
    assert load_root_axes_binding()["config_sha256"] == hashlib.sha256(original).hexdigest()
    path.unlink()
    with pytest.raises(BrainstormGenerationError):
        load_root_axes_binding()


def test_default_stage_capabilities_do_not_invent_direction_detail_attributes():
    compiler = RootAxisCompiler(binding())
    assert [axis["id"] for axis in compiler.eligible_axes("direction")] == [
        "audience", "purpose", "content_focus", "narrative_perspective", "tone"]
    assert tuple(axis["id"] for axis in compiler.eligible_axes("detail")) == ROOT_IDS


@pytest.mark.parametrize("change", ["result", "hash", "unknown_id", "mixed", "stage", "topic", "limit"])
def test_program_root_proof_is_recompiled_before_semantic_protocol(change):
    value = binding()
    raw = {"root_axis_ids": ["audience", "information_granularity"]}
    budget = BrainstormStageCoordinator.limits({"max_topics": 1})
    manifest = axis_plan_manifest("detail", raw["root_axis_ids"], budget)
    selected = copy.deepcopy(manifest["topics"][0])
    result = RootAxisCompiler(value).topic_goals(selected, "detail", budget, 2, manifest["topics"])
    task = {"max_root_goals": 2, "program_bound_root_axes": {"binding": value,
        "stage": "detail", "root_limit": 2, "topic": copy.deepcopy(selected),
        "topics": copy.deepcopy(manifest["topics"]), "limits": budget}}
    BrainstormStageCoordinator._verify_root_axis_compilation(stage="detail", kind="topic_plan",
        topic=selected, task=task, result=result)
    proof = task["program_bound_root_axes"]
    if change == "result":
        result["root_goals"][0] = "与注册属性无关的自由正文目标"
    elif change == "hash":
        proof["binding"]["config_sha256"] = "0" * 64
    elif change == "unknown_id":
        proof["topic"]["root_axis_ids"][0] = "audience_alias"
    elif change == "mixed":
        proof["topic"]["root_goals"] = result["root_goals"]
    elif change == "stage":
        proof["stage"] = "direction"
    elif change == "topic":
        proof["topic"]["goal"] = "另一个主题目标"
    else:
        proof["root_limit"] = 1
    with pytest.raises(BrainstormGenerationError):
        BrainstormStageCoordinator._verify_root_axis_compilation(stage="detail", kind="topic_plan",
            topic=selected, task=task, result=result)


@pytest.mark.asyncio
async def test_root_mapping_is_reverified_after_semantic_callback_mutates_result():
    coordinator, service = root_service({"root_axis_ids": ["audience"]})
    async def mutate(**kwargs):
        kwargs["result"]["root_goals"][0] = "伪造自由根目标"
    coordinator._check_stage_consistency = mutate
    with pytest.raises(BrainstormGenerationError):
        await request_roots(coordinator, root_axes_binding=binding())
    assert service.model_calls == []


def test_free_node_prompt_requires_real_mutual_exclusion_for_single_choice():
    system = BrainstormStageCoordinator._node_scope_prompt("node")
    assert "multi_choice" in system and "single_choice" in system
    assert "互斥" in system and "single_choice_reason" in system
    assert "同一主题" in system and "先后" in system


@pytest.mark.asyncio
async def test_bound_free_node_rejects_changed_registry_before_provider_or_recall():
    from tests.test_brainstorm_stage_nodes import candidate, prepare, setup
    coordinator, service = setup([candidate()])
    coordinator._retrieve_memory = lambda *args: (_ for _ in ()).throw(AssertionError("invalid binding must fail before recall"))
    value = binding()
    value["config_sha256"] = "0" * 64
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, root_axes_binding=value)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_legacy_node_without_new_binding_preserves_goal_parents_and_sources_without_registry_reinterpretation(monkeypatch):
    from tests.test_brainstorm_stage_nodes import candidate, node, prepare, setup
    from tests.test_brainstorm_stage_citations import MEMORY
    selected_node = node(2)
    assert selected_node["goal"] not in {axis["goal"] for axis in binding()["config"]["axes"]}
    def forbidden(*args, **kwargs):
        pytest.fail("protected legacy work without new binding must not enter the new registry")
    monkeypatch.setattr(STAGES, "RootAxisCompiler", forbidden)
    coordinator, service = setup([candidate(2, choice="reference:m1+m2")], MEMORY)
    result = await prepare(coordinator, node=selected_node)
    assert result["question"]["id"] == selected_node["id"]
    assert result["question"]["parent_question_id"] == selected_node["parent_question_id"]
    assert result["next_goals"] == []
    generated = json.loads(service.prompts[0])
    assert generated["generation_task"]["node_goal"] == selected_node["goal"]
    assert "current_axis" not in generated["generation_task"]
    assert generated["memory"] == MEMORY
    referenced = next(option for option in result["question"]["options"] if option["id"] == "a")
    assert all(source["content"] in referenced["details"] for source in MEMORY["sources"][:2])
    assert len(service.model_calls) == 1
