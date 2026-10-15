"""Atomic roots terminate by verified registry proof, never model permission."""
import copy
import json
import asyncio
import importlib

import httpx
import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_root_axes import RootAxisCompiler, load_root_axes_binding
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.brainstorm_axis_fixtures import axis_plan_manifest
from tests.test_brainstorm_stage_citations import MEMORY, NO_MEMORY
from tests.test_brainstorm_stage_nodes import check_result, fixed_decision, setup


def atomic_request(stage="direction", identifiers=None, ordinal=0, limits=None):
    identifiers = identifiers or ["audience"]
    budget = Coordinator.limits(limits)
    manifest = axis_plan_manifest(stage, identifiers, budget)
    selected = manifest["topics"][0]
    identifier = selected["root_axis_ids"][ordinal]
    axes = {axis["id"]: axis for axis in load_root_axes_binding()["config"]["axes"]}
    node_id = "stage-atomic-test-1-{}".format(ordinal)
    return {"stage": stage, "kind": "node", "root_request": "周报受众未确定，保留全部必需内容和政策",
        "decisions": [], "context_id": "a" * 64, "root_axes_binding": load_root_axes_binding(),
        "topic": selected, "plan": {"topics": manifest["topics"]}, "limits": budget,
        "node": {"id": node_id, "goal": axes[identifier]["goal"], "depth": 1,
            "parent_question_id": None, "parent_option_id": None, "ancestor_conditions": [],
            "max_options": 5, "max_next_goals": 0},
        "node_mode": {"kind": "root_axis_atomic", "axis_id": identifier, "node_id": node_id}}


def raw_value_selection(axis="audience"):
    # Explicit implemented values, not a translation of arbitrary old copy.
    identifiers = {
        "audience": ["audience.participants", "audience.decision_makers"],
        "purpose": ["purpose.form_judgement", "purpose.encourage_action"],
    }[axis]
    return {"options": [{"value_id": identifier, "recommended": index == 0,
        "memory_choice": "original"} for index, identifier in enumerate(identifiers)]}


def test_registry_declares_atomic_property_modes_without_arbitrary_descendant_capabilities():
    value = load_root_axes_binding()
    assert value["config"]["registry_version"] == 4
    assert all(axis["decision_mode"] == "atomic" for axis in value["config"]["axes"])
    assert len({axis["id"] for axis in value["config"]["axes"]}) == 11


@pytest.mark.parametrize("depth_ceiling", [2, 3])
@pytest.mark.asyncio
async def test_depth_ceiling_does_not_force_registered_atomic_roots_to_invent_children(depth_ceiling):
    request = atomic_request(limits={"max_depth": depth_ceiling})
    c, service = setup([raw_value_selection()])
    frozen = copy.deepcopy(request)
    result = await c.stage_batch(**request)
    assert result["next_goals"] == []
    assert result["question"]["id"] == request["node"]["id"]
    assert result["question"]["parent_question_id"] is None
    assert result["question"]["dimension_id"] == request["topic"]["id"]
    assert len(result["question"]["options"]) == 2
    assert request == frozen and len(service.model_calls) == 1
    schema = service.model_calls[0]["json_schema"]["properties"]
    assert set(schema) == {"options"}
    assert set(schema["options"]["items"]["properties"]) == {"value_id", "recommended", "memory_choice"}
    assert result["root_value_selection"] == raw_value_selection()
    assert not {"node_mode", "terminal", "is_terminal", "question", "next_goals"}.intersection(schema)


@pytest.mark.parametrize("change", ["boolean_mode", "wrong_kind", "wrong_axis", "wrong_packet_node", "extra_mode",
    "missing_mode_axis", "wrong_goal", "empty_goal", "wrong_ordinal", "nonzero_children_budget",
    "caller_terminal", "node_extra_mode", "wrong_topic_title", "topic_extra", "whole_plan_extra", "bad_config",
    "depth_two", "parent", "ancestor", "omitted_topic_root", "missing_binding"])
@pytest.mark.asyncio
async def test_untrusted_terminal_claim_or_binding_drift_cannot_grant_atomic_completion(change):
    request = atomic_request()
    if change == "boolean_mode":
        request["node_mode"] = True
    elif change == "wrong_kind":
        request["node_mode"]["kind"] = "model_terminal"
    elif change == "wrong_axis":
        request["node_mode"]["axis_id"] = "purpose"
    elif change == "wrong_packet_node":
        request["node_mode"]["node_id"] += "-other"
    elif change == "extra_mode":
        request["node_mode"]["terminal"] = True
    elif change == "missing_mode_axis":
        request["node_mode"].pop("axis_id")
    elif change == "wrong_goal":
        request["node"]["goal"] = "选择需要展示的收益指标"
    elif change == "empty_goal":
        request["node"].pop("goal")
    elif change == "wrong_ordinal":
        request["node"]["id"] = request["node_mode"]["node_id"] = "stage-atomic-test-1-1"
    elif change == "nonzero_children_budget":
        request["node"]["max_next_goals"] = 2
    elif change == "caller_terminal":
        request["node_mode"] = None
        request["node"]["terminal"] = True
    elif change == "node_extra_mode":
        request["node"]["decision_mode"] = "atomic"
    elif change == "wrong_topic_title":
        request["topic"]["title"] = "自由事实核验主题"
    elif change == "topic_extra":
        request["topic"]["chapter_id"] = ""
    elif change == "whole_plan_extra":
        request["plan"]["topics"][0]["free_goal"] = "重新决定数据来源"
    elif change == "depth_two":
        request["node"]["depth"] = 2
    elif change == "parent":
        request["node"].update(parent_question_id="old-root", parent_option_id="a")
    elif change == "ancestor":
        request["node"]["ancestor_conditions"] = [{"parent_question_id": "old-root", "parent_option_id": "a"}]
    elif change == "omitted_topic_root":
        request["topic"]["root_axis_ids"] = []
    elif change == "missing_binding":
        request.pop("root_axes_binding")
    else:
        request["root_axes_binding"]["config"]["axes"][0]["decision_mode"] = "branching"
    c, service = setup([])
    c._retrieve_memory = lambda *args: pytest.fail("invalid mode must reject before recall")
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**request)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_zero_future_budget_without_program_mode_does_not_disable_legacy_tree_coverage():
    request = atomic_request()
    request.pop("node_mode")
    request.pop("root_axes_binding")
    c, service = setup([])
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**request)
    assert service.model_calls == []


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.asyncio
async def test_new_atomic_binding_cannot_omit_mode_and_fall_back_to_positive_budget_legacy_generation(depth):
    request = atomic_request()
    request.pop("node_mode")
    request["node"].update(max_options=2, max_next_goals=2)
    if depth == 2:
        request["limits"]["max_depth"] = 3
        request["node"].update(depth=2, parent_question_id="saved-parent", parent_option_id="a",
            ancestor_conditions=[{"parent_question_id": "saved-parent", "parent_option_id": "a",
                "parent_prompt": "选择哪种阅读对象？", "option_label": "技术团队", "option_description": "实际原阅读对象"}])
    # Prove this is a valid pre-existing tree request. The new missing-mode
    # guard, rather than a leaf/depth/branch-capacity error, must reject it.
    Coordinator._node_request(copy.deepcopy(request["node"]), request["limits"])
    c, service = setup([raw_value_selection()])
    c._retrieve_memory = lambda *args: pytest.fail("missing mode must reject before recall")
    async def forbidden(**kwargs):
        pytest.fail("missing mode must reject before semantic review")
    c._check_stage_consistency = forbidden
    before = copy.deepcopy(request)
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**request)
    assert service.model_calls == [] and request == before


@pytest.mark.parametrize("changed", ["option_goals", "provider_terminal"])
@pytest.mark.asyncio
async def test_atomic_provider_cannot_add_children_or_its_own_terminal_claim(changed):
    raw = raw_value_selection()
    if changed == "option_goals":
        raw["options"][0]["next_goals"] = ["选择具体指标维度"]
    else:
        raw["terminal"] = True
    c, service = setup([raw, raw])
    frozen = copy.deepcopy(raw)
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**atomic_request())
    assert len(service.model_calls) == Coordinator.MAX_NODE_GENERATION_ATTEMPTS and raw == frozen


@pytest.mark.asyncio
async def test_atomic_root_preserves_actual_memory_recall_and_all_bound_source_text():
    raw = raw_value_selection()
    raw["options"][0]["memory_choice"] = "reference:m1+m2"
    c, service = setup([raw], MEMORY)
    calls = []
    c._retrieve_memory = lambda *args: calls.append(args) or copy.deepcopy(MEMORY)
    result = await c.stage_batch(**atomic_request())
    assert len(calls) == 1 and result["next_goals"] == []
    referenced = next(option for option in result["question"]["options"] if option["id"] == "a")
    assert all(source["content"] in referenced["details"] for source in MEMORY["sources"][:2])
    context = json.loads(service.prompts[0])
    assert context["memory"] == MEMORY
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_atomic_structural_completion_keeps_full_root_skill_fixed_and_semantic_guards():
    request = atomic_request()
    root = "完整原始需求" * 1000
    skills = [{"id": "policy", "skillDescription": {"rules": ["所有原规则" * 1500]}, "policy_tail": "末尾不可丢"}]
    decision = fixed_decision()
    request.update(root_request=root, selected_skills=skills, decisions=[decision])
    c, service = setup([raw_value_selection()])
    seen = []
    async def inspect(**kwargs):
        assert kwargs["task"]["user_constraints"]["root_request"] == root
        assert kwargs["task"]["user_constraints"]["selected_skills"] == skills
        assert kwargs["task"]["fixed_decisions"][0]["answer"] == decision["answer"]
        assert kwargs["task"]["node_goal"] == request["node"]["goal"]
        assert kwargs["result"]["next_goals"] == []
        seen.append(copy.deepcopy(kwargs))
    c._check_stage_consistency = inspect
    before = copy.deepcopy(request)
    result = await c.stage_batch(**request)
    assert len(seen) == 1 and request == before and result["next_goals"] == []
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_atomic_root_semantic_rejection_still_rejects_current_question():
    from creation.brainstorm_stages import _StageConsistencyError
    raw = raw_value_selection()
    units = ["q0_prompt", "q0_space", "q0_o0", "q0_o1"]
    rejected = check_result("reject", "topic_scope", unit_ids=units,
        option_labels={"a": "执行参与者", "b": "管理决策者"})
    c, service = setup([raw, rejected, raw, rejected], check_consistency=True)
    with pytest.raises((BrainstormGenerationError, _StageConsistencyError)):
        await c.stage_batch(**atomic_request())
    assert [call["num_predict"] for call in service.model_calls] == [2400, 1200, 2400, 1200]
    checked = json.loads(service.prompts[1])
    assert len(checked["validation_task"]["units"]) == 4
    assert checked["question_contexts"]["q0"]["node_goal"] == atomic_request()["node"]["goal"]


@pytest.mark.asyncio
async def test_atomic_post_gate_verification_rejects_added_future_goal_even_after_pass():
    c, service = setup([raw_value_selection(), raw_value_selection()])
    async def mutate(**kwargs):
        kwargs["result"]["next_goals"] = [{"option_id": "a", "goals": ["越轴子目标"]}]
    c._check_stage_consistency = mutate
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(**atomic_request())
    assert len(service.model_calls) == Coordinator.MAX_NODE_GENERATION_ATTEMPTS


def test_atomic_plan_capacity_preserves_five_roots_in_one_topic_despite_depth_three_ceiling():
    value = load_root_axes_binding()
    c = RootAxisCompiler(value)
    identifiers = [axis["id"] for axis in c.eligible_axes("direction")]
    budget = Coordinator.limits({"max_topics": 1, "max_depth": 3, "max_questions_per_topic": 5})
    plan = c.compile_plan({"axis_ids": identifiers}, "direction", budget)
    assert len(plan["topics"]) == 1 and plan["topics"][0]["root_axis_ids"] == identifiers
    roots = c.topic_goals(plan["topics"][0], "direction", budget, 5, plan["topics"])
    assert len(roots["root_goals"]) == 5


@pytest.mark.asyncio
async def test_grouped_atomic_root_uses_its_original_ordinal_and_preserves_other_attributes():
    request = atomic_request(identifiers=["audience", "purpose"], ordinal=1, limits={"max_topics": 1})
    raw = raw_value_selection("purpose")
    c, service = setup([raw])
    seen = []
    async def inspect(**kwargs):
        assert kwargs["task"]["current_axis"]["id"] == "purpose"
        assert kwargs["task"]["program_bound_atomic_root"]["topics"] == request["plan"]["topics"]
        seen.append(kwargs["task"]["node_goal"])
    c._check_stage_consistency = inspect
    frozen = copy.deepcopy(request)
    result = await c.stage_batch(**request)
    assert result["next_goals"] == [] and result["question"]["id"].endswith("-1")
    assert request == frozen and request["topic"]["root_axis_ids"] == ["audience", "purpose"]
    assert seen == [request["node"]["goal"]] and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_atomic_mode_cannot_enter_existing_writing_or_installed_catalog_paths(tmp_path):
    from tests.test_brainstorm_stage_nodes import prepare
    from tests.test_brainstorm_catalog import directory, install, request as catalog_request, execution_node
    packet = atomic_request()["node_mode"]
    c, service = setup([])
    with pytest.raises(BrainstormGenerationError):
        await prepare(c, stage="writing", node_mode=packet)
    assert service.model_calls == []
    catalog, catalog_service, binding, skill = install(tmp_path, directory())
    plan = await catalog_request(catalog, binding, skill)
    root = binding["catalog"]["stages"]["detail"]["slots"][0]
    with pytest.raises(BrainstormGenerationError):
        await catalog_request(catalog, binding, skill, kind="node", topic=plan["topics"][0],
            node=execution_node(root), node_mode=packet)
    assert catalog_service.model_calls == []


def atomic_proof(request):
    return {"binding": copy.deepcopy(request["root_axes_binding"]), "stage": request["stage"],
        "topic": copy.deepcopy(request["topic"]), "topics": copy.deepcopy(request["plan"]["topics"]),
        "limits": copy.deepcopy(request["limits"]), "node": copy.deepcopy(request["node"]),
        "node_mode": copy.deepcopy(request["node_mode"])}


@pytest.mark.parametrize("change", ["hash", "axis", "goal", "ordinal", "stage", "whole_plan", "omitted_root", "extra"])
def test_decoder_and_internal_binders_recheck_program_proof_instead_of_trusting_a_terminal_boolean(change):
    request = atomic_request()
    proof = atomic_proof(request)
    schema = Coordinator._node_schema(request["node"], NO_MEMORY, request["limits"], atomic_root_proof=proof)
    assert set(schema["properties"]) == {"options"}
    assert "next_goals" not in schema["properties"]["options"]["items"]["properties"]
    if change == "hash":
        proof["binding"]["config_sha256"] = "0" * 64
    elif change == "axis":
        proof["node_mode"]["axis_id"] = "purpose"
    elif change == "goal":
        proof["node"]["goal"] = "脱离本属性的自由目标"
    elif change == "ordinal":
        proof["node_mode"]["node_id"] += "-fake"
    elif change == "stage":
        proof["stage"] = "writing"
    elif change == "whole_plan":
        proof["topics"] = []
    elif change == "omitted_root":
        proof["topic"]["root_axis_ids"] = []
    else:
        proof["terminal"] = True
    for operation in (lambda: Coordinator._node_schema(request["node"], NO_MEMORY, request["limits"], atomic_root_proof=proof),
        lambda: Coordinator._compile_provider_node_candidate(raw_value_selection(), request["node"], request["limits"], atomic_root_proof=proof, memory=NO_MEMORY)):
        with pytest.raises(BrainstormGenerationError):
            operation()


@pytest.mark.parametrize("change", ["node_id", "parent", "dimension", "goals", "task_goal", "current_axis", "binding", "mode"])
def test_atomic_post_semantic_proof_rejects_identity_or_goal_changes(change):
    request = atomic_request()
    proof = atomic_proof(request)
    selection = raw_value_selection()
    result = Coordinator._render_root_value_selection(proof, selection, NO_MEMORY, [], [])
    axis = RootAxisCompiler(request["root_axes_binding"]).atomic_axis(request["node"], request["stage"], request["limits"],
        request["topic"], request["plan"]["topics"], request["node_mode"])
    task = {"program_bound_atomic_root": proof, "current_axis": axis, "node_goal": request["node"]["goal"],
        "depth": 1, "max_next_goals": 0, "ancestor_conditions": [], "max_options": request["node"]["max_options"],
        "current_value_capability": RootAxisCompiler(request["root_axes_binding"]).value_capability(axis),
        "program_bound_root_values": {"selection": copy.deepcopy(selection), "memory": copy.deepcopy(NO_MEMORY),
            "previous": [], "existing_bodies": []}}
    Coordinator._verify_atomic_root_compilation(stage="direction", kind="node", topic=request["topic"], task=task, result=result)
    if change == "node_id":
        result["question"]["id"] += "-fake"
    elif change == "parent":
        result["question"].update(parent_question_id="fake-parent", parent_option_id="a")
    elif change == "dimension":
        result["question"]["dimension_id"] = "topic_axis_2"
    elif change == "goals":
        result["next_goals"] = [{"option_id": "a", "goals": ["自由新子目标"]}]
    elif change == "task_goal":
        task["node_goal"] = "被省略或改写的根目标"
    elif change == "current_axis":
        task["current_axis"]["decision_mode"] = "model_terminal"
    elif change == "binding":
        proof["binding"]["config_sha256"] = "0" * 64
    else:
        proof["node_mode"]["kind"] = "model_terminal"
    with pytest.raises(BrainstormGenerationError):
        Coordinator._verify_atomic_root_compilation(stage="direction", kind="node", topic=request["topic"], task=task, result=result)


@pytest.mark.asyncio
async def test_api_forwards_exact_atomic_packet_without_replacing_proof_by_a_boolean(monkeypatch):
    from tests.test_creation_queue import BrainstormDeadlineThreadQueue
    app = importlib.import_module("creation.app")
    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    captured = []
    async def stage_batch(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        return {"question": {"id": kwargs["node"]["id"]}, "next_goals": []}
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    payload = atomic_request()
    before = copy.deepcopy(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json=payload)
    assert response.status_code == 200 and response.json()["next_goals"] == []
    assert len(captured) == 1
    for field in ("node_mode", "root_axes_binding", "topic", "plan", "node"):
        assert captured[0][field] == payload[field]
    assert payload == before
    await asyncio.to_thread(queue.worker.join, 1)
    assert not queue.worker.is_alive()
