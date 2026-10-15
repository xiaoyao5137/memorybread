"""Limit new writing choices without rewriting saved or trusted branches."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.test_brainstorm_catalog import directory, execution_node, install, request
from tests.test_brainstorm_stage_nodes import candidate, node, prepare, review_record, setup
from tests.test_brainstorm_stages import plan
from tests.test_brainstorm_writing_scope import compiled_writing_node, writing_candidate


ENV = "MEMORYBREAD_BRAINSTORM_WRITING_MAX_OPTIONS"


def generated(count, depth=1, maximum=2):
    value = {"question": writing_candidate(depth, maximum)["question"]}
    value["question"]["options"] = [{"label": "按线索{}组织".format(index + 1),
        "description": "沿用本章对象，采用第{}种独立说明组织方法。".format(index + 1),
        "recommended": index == count - 1, "memory_choice": "original"} for index in range(count)]
    return value


def requested(core=5, depth=1, maximum=2, goals=5):
    value = node(depth, maximum)
    value.update(goal=Coordinator.WRITING_AXES[0]["levels"][depth - 1], max_options=core,
        max_next_goals=0 if depth == maximum else goals)
    return value


@pytest.mark.parametrize("value,expected", [(None, 2), ("", 2), ("bad", 2), ("true", 2), ("1", 2),
    ("6", 2), ("0", 2), ("-2", 2), ("2.5", 2), ("2", 2), ("3", 3), ("4", 4), ("5", 5), (" 4 ", 4)])
def test_runtime_writing_cap_is_bounded_and_invalid_configuration_uses_default(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert Coordinator.writing_max_options() == expected


@pytest.mark.parametrize("configured", [2, 3, 4, 5])
@pytest.mark.parametrize("core", [2, 3, 4, 5])
@pytest.mark.asyncio
async def test_schema_task_and_all_new_paths_bind_the_minimum_real_budget(monkeypatch, configured, core):
    monkeypatch.setenv(ENV, str(configured))
    actual = min(configured, core)
    raw, trusted = generated(actual), requested(core)
    previous = {"prepared_question_prompts": ["已保存的独立章节问题"],
        "chapters": [{"id": "chapter1", "title": "当前章节", "goal": "解释本章人物与事件"}]}
    before = copy.deepcopy((raw, trusted, previous))
    coordinator, service = setup([raw])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs))
    coordinator._check_stage_consistency = inspect
    result = await prepare(coordinator, stage="writing", node=trusted, plan=previous)
    assert (raw, trusted, previous) == before
    assert len(seen) == 1
    task = seen[0]["task"]
    assert task["max_options"] == actual
    assert task["max_next_goals"] == trusted["max_next_goals"]
    assert len(result["question"]["options"]) == len(result["next_goals"]) == 2
    assert {group["option_id"] for group in result["next_goals"]} == {option["id"] for option in result["question"]["options"]}
    assert all(group["goals"] == [Coordinator.WRITING_AXES[0]["levels"][1]] for group in result["next_goals"])
    assert result["question"]["options"][0]["recommended"] is True
    assert task["editorial_compilation"]["inputs"]["node"]["max_options"] == actual
    assert task["editorial_compilation"]["packet"]["next_goals"] == result["next_goals"]
    Coordinator._verify_editorial_compilation(result=result, task=task, topic=seen[0]["topic"])
    assert service.model_calls == [] and len(service.responses) == 1


@pytest.mark.parametrize("depth,maximum", [(1, 3), (2, 3), (3, 3), (2, 2)])
@pytest.mark.asyncio
async def test_actual_children_and_leaf_keep_all_true_conditions_under_the_new_cap(monkeypatch, depth, maximum):
    monkeypatch.delenv(ENV, raising=False)
    trusted = compiled_writing_node(depth, maximum)
    coordinator, service = setup([generated(2, depth, maximum)])
    result = await prepare(coordinator, stage="writing", node=trusted, limits={"max_depth": maximum})
    for condition in trusted["ancestor_conditions"]:
        assert condition["option_label"] in result["question"]["prompt"]
    assert len(result["question"]["options"]) == 2
    assert len(result["next_goals"]) == (0 if depth == maximum else 2)
    assert service.model_calls == [] and len(service.responses) == 1


@pytest.mark.asyncio
async def test_model_exceeding_derived_cap_is_rejected_whole_without_slicing(monkeypatch):
    monkeypatch.setenv(ENV, "2")
    raw = generated(5)
    before = copy.deepcopy(raw)
    selected = {"id": "topic", "title": "当前章节", "goal": Coordinator.WRITING_AXES[0]["goal"],
        "chapter_id": "chapter1"}
    with pytest.raises(BrainstormGenerationError, match="选项数量"):
        Coordinator._bind_node_candidate(raw, requested(core=2), selected, "writing", Coordinator.DEFAULT_LIMITS,
            writing_axis=Coordinator.WRITING_AXES[0])
    coordinator, service = setup([raw, raw])
    result = await prepare(coordinator, stage="writing", node=requested())
    assert raw == before and len(raw["question"]["options"]) == 5
    assert len(result["question"]["options"]) == 2
    assert service.model_calls == [] and len(service.responses) == 2


@pytest.mark.asyncio
async def test_program_future_capacity_does_not_charge_writing_generation_tokens(monkeypatch):
    monkeypatch.setenv(ENV, "5")
    coordinator, service = setup([generated(5)])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs))
    coordinator._check_stage_consistency = inspect
    result = await prepare(coordinator, stage="writing", node=requested(goals=23),
        limits={"max_questions_per_topic": 24})
    assert len(result["next_goals"]) == 2 and service.model_calls == [] and len(service.responses) == 1
    assert len(seen) == 1 and seen[0]["task"]["max_next_goals"] == 23
    assert seen[0]["task"]["editorial_compilation"]["inputs"]["node"]["max_next_goals"] == 23


@pytest.mark.parametrize("stage", ["direction", "detail"])
@pytest.mark.asyncio
async def test_other_stages_retain_original_option_and_goal_output_budget(monkeypatch, stage):
    monkeypatch.setenv(ENV, "2")
    raw = candidate()
    for index in range(3):
        raw["question"]["options"].append({"label": "从线索{}入手".format(index + 1),
            "description": "通过独立线索{}展开本章".format(index + 1), "recommended": False,
            "memory_choice": "original"})
        raw["next_goals"].append({"option_index": index + 3, "goal": "比较线索{}节奏".format(index + 1)})
    trusted = {**node(), "max_options": 5, "max_next_goals": 6}
    coordinator, service = setup([raw])
    result = await prepare(coordinator, stage=stage, node=trusted)
    assert len(result["question"]["options"]) == 5
    assert service.model_calls[0]["num_predict"] == Coordinator._node_output_tokens(trusted)
    assert service.model_calls[0]["num_predict"] > 2400
    assert json.loads(service.prompts[0])["generation_task"]["max_options"] == 5


@pytest.mark.parametrize("stage", ["detail", "writing"])
@pytest.mark.asyncio
async def test_saved_five_option_question_is_reviewed_intact_under_default_cap(monkeypatch, stage):
    monkeypatch.setenv(ENV, "2")
    saved = review_record()
    if stage == "writing":
        saved["topic"].update(chapter_id="chapter1", goal=Coordinator.WRITING_AXES[0]["goal"])
    for index in range(3):
        saved["question"]["options"].append({"id": chr(ord("c") + index),
            "label": "保存的组织方法{}".format(index + 1), "description": "已保存的完整方案{}".format(index + 1),
            "recommended": False})
    before = copy.deepcopy(saved)
    coordinator, service = setup([])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs["result"]["review_questions"]))
    coordinator._check_stage_consistency = inspect
    result = await prepare(coordinator, kind="review", stage=stage, node=None,
        plan={"topics": [saved["topic"]]}, review_questions=[saved])
    assert result["review"] == "pass" and saved == before
    assert seen == [[before]] and service.model_calls == []


@pytest.mark.asyncio
async def test_generation_cap_cannot_repair_or_mask_an_invalid_core_branch_budget(monkeypatch):
    monkeypatch.setenv(ENV, "2")
    invalid = requested(core=5, goals=2)
    before = copy.deepcopy(invalid)
    coordinator, service = setup([])
    with pytest.raises(BrainstormGenerationError, match="完整覆盖"):
        await prepare(coordinator, stage="writing", node=invalid)
    assert invalid == before and service.model_calls == []


@pytest.mark.asyncio
async def test_trusted_writing_catalog_keeps_all_five_configured_branches(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "2")
    value = directory(stages=("writing",))
    slots = value["stages"]["writing"]["slots"]
    root = slots[0]
    for index in range(2, 5):
        child = copy.deepcopy(slots[1])
        child["id"] = "child_{}".format(index)
        child["prompt_body"] = "第{}种记录的说明按什么顺序组织？".format(index + 1)
        slots.append(child)
        method = copy.deepcopy(root["methods"][0])
        method.update(id="method_{}".format(index), label="独立记录方法{}".format(index + 1),
            description="保留完整原字段，采用独立记录方法{}。".format(index + 1), child_slot_ids=[child["id"]])
        root["methods"].append(method)
    coordinator, service, binding, skill = install(tmp_path, value)
    selected = (await request(coordinator, binding, skill, stage="writing"))["topics"][0]
    result = await request(coordinator, binding, skill, stage="writing", kind="node", topic=selected,
        node=execution_node(binding["catalog"]["stages"]["writing"]["slots"][0]))
    assert len(result["question"]["options"]) == len(result["next_goals"]) == 5
    assert service.model_calls == []


@pytest.mark.parametrize("stage,kind", [(stage, kind) for stage in ("direction", "detail", "writing")
    for kind in ("plan", "node", "review")] + [("direction", "topic_plan"), ("detail", "topic_plan")])
@pytest.mark.asyncio
async def test_each_semantic_task_receives_all_full_selected_skill_profiles(stage, kind):
    skills = [{"id": "selected_skill_{}".format(index), "title": "完整Skill{}".format(index),
        "skillDescription": {"rules": ["原始完整规则" * 1000]}, "extra_profile": {"rules": ["末尾不可遗漏约束"]}}
        for index in range(6)]
    before = copy.deepcopy(skills)
    from tests.brainstorm_axis_fixtures import axis_plan_raw
    raw = (plan(stage) if stage == "writing" else axis_plan_raw(stage)) if kind == "plan" else {} if kind == "topic_plan" else (
        generated(2) if stage == "writing" else candidate())
    coordinator, service = setup([] if kind == "review" else [raw])
    seen = []
    async def inspect(**kwargs):
        full = kwargs["task"]["user_constraints"]["selected_skills"]
        assert full == before and full is not skills and full[0]["skillDescription"] is not skills[0]["skillDescription"]
        seen.append(copy.deepcopy(full))
        independent = copy.deepcopy(full)
        independent[-1]["extra_profile"]["rules"].append("测试修改仅作用于独立校验副本")
    coordinator._check_stage_consistency = inspect
    coordinator._check_plan_consistency = inspect
    extra = {"kind": kind, "selected_skills": skills}
    if kind == "topic_plan":
        extra.update(node=None, root_limit=1)
    elif kind == "review":
        saved = review_record()
        if stage == "writing":
            saved["topic"].update(chapter_id="chapter1", goal=Coordinator.WRITING_AXES[0]["goal"])
        extra.update(node=None, review_questions=[saved], plan={"topics": [saved["topic"]]})
    await prepare(coordinator, stage=stage, **extra)
    assert skills == before and seen == [before]
