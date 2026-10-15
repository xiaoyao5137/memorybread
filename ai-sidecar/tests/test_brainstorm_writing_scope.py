"""Model output cannot turn confirmed writing content into execution goals."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_editorial import EditorialCompiler
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.test_brainstorm_stage_nodes import candidate, node, prepare, setup
from tests.test_brainstorm_stages import plan, topic


def writing_candidate(depth=1, max_depth=2):
    value = candidate(depth, max_depth)
    for field in ("single_choice_reason", "answer_template"):
        value["question"].pop(field)
    return value


def compiled_writing_node(depth=1, maximum=2, core=5, goals=5):
    """Use real compiled parent copy, rather than invented legacy ancestors."""
    axis = Coordinator.WRITING_AXES[0]
    selected = topic()
    selected.update(chapter_id="chapter1", goal=axis["goal"])
    chapter = {"id": "chapter1", "title": "人物与事件说明",
        "goal": "交代本章人物和事件，不承担其余章节主任务"}
    authority = {"fixed_decisions": [], "cleared_constraints": [],
        "user_constraints": {"root_request": "构思人物故事", "brief_edits": {}, "selected_skills": []}}
    ancestors = []
    for level in range(1, depth):
        parent = node(level, maximum)
        parent.update(id="parent{}".format(level - 1), goal=axis["levels"][level - 1], max_options=core,
            max_next_goals=goals, ancestor_conditions=copy.deepcopy(ancestors),
            parent_question_id=ancestors[-1]["parent_question_id"] if ancestors else None,
            parent_option_id="a" if ancestors else None)
        packet = EditorialCompiler.compile(chapter=chapter, chapters=[chapter], topic=selected,
            node=parent, axis=axis, authority=authority, selected_skills=[], max_depth=maximum,
            confirmed_writing_choices=[])
        option = packet["parsed"]["question"]["options"][0]
        ancestors.append({"parent_question_id": parent["id"], "parent_option_id": "a",
            "parent_prompt": Coordinator._node_condition_prefix(parent) + packet["parsed"]["question"]["prompt_body"],
            "option_label": option["label"], "option_description": option["description"]})
    requested = node(depth, maximum)
    requested.update(goal=axis["levels"][depth - 1], max_options=core,
        max_next_goals=0 if depth == maximum else goals, ancestor_conditions=ancestors,
        parent_question_id=ancestors[-1]["parent_question_id"] if ancestors else None,
        parent_option_id="a" if ancestors else None)
    return requested


@pytest.mark.parametrize("goal", [
    "确立报告时间窗口，明确数据源优先级并定义灰色缺失标注规范",
    "按已经确认的年化成本降序展示前十项，并重新决定图注位置",
    "设立已经确认的独立风险板块，选择已经确定的证据摘要先行",
    "建立故事开端",
])
def test_opaque_content_goal_never_becomes_writing_task_even_with_a_global_pass(goal):
    value = plan("writing")
    value["topics"][0]["goal"] = goal
    original = copy.deepcopy(value)
    with pytest.raises(BrainstormGenerationError, match="组织能力"):
        Coordinator._validate_plan(value, "writing", Coordinator.DEFAULT_LIMITS)
    assert value == original


@pytest.mark.asyncio
async def test_writing_outline_has_no_unsourced_recall_and_keeps_complete_user_authority():
    value = Coordinator._atomic_writing_plan({})
    coordinator, service = setup([])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs))
    coordinator._check_plan_consistency = inspect
    async def forbidden_recall(*args, **kwargs):
        pytest.fail("A writing outline must not import new historical facts")
    coordinator._stage_memory = forbidden_recall
    decisions = [{"question_id": "period", "question": "报告周期？", "answer": "使用已确认周期",
        "answer_source": "user", "user_inputs": ["沿用原周期，不重新选择"]}]
    result = await prepare(coordinator, stage="writing", kind="plan", decisions=decisions)
    assert result["topics"] == value["topics"]
    assert seen[0]["task"]["fixed_decisions"][0]["answer"] == decisions[0]["answer"]
    assert seen[0]["task"]["fixed_decisions"][0]["user_inputs"] == decisions[0]["user_inputs"]
    assert seen[0]["result"] == value and not service.model_calls


@pytest.mark.parametrize("axis", Coordinator.WRITING_AXES)
@pytest.mark.asyncio
async def test_writing_root_is_compiled_without_model_or_recall(axis):
    coordinator, service = setup([])
    coordinator._retrieve_memory = lambda *args: pytest.fail("compiled roots cannot retrieve")
    result = await prepare(coordinator, stage="writing", kind="topic_plan", node=None,
        topic={"id": "topic", "title": "通用章节", "goal": axis["goal"], "chapter_id": "chapter1"}, root_limit=4)
    assert result == {"root_goals": [axis["levels"][0]]}
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_old_private_writing_node_cannot_issue_a_model_request():
    coordinator, service = setup([])
    with pytest.raises(BrainstormGenerationError, match="可信组织路径"):
        await prepare(coordinator, stage="writing", node={**node(), "goal": "重新确认已选择的数据来源"})
    assert service.model_calls == []


@pytest.mark.parametrize("goal", ["重新确认灰色缺失标注", "重新决定数据来源优先级", "安排人物出场"])
@pytest.mark.asyncio
async def test_child_scope_cannot_be_reopened_by_an_otherwise_valid_model_candidate(goal):
    invalid = writing_candidate()
    invalid["next_goals"] = [{"option_index": index, "goal": goal} for index in (1, 2)]
    requested = compiled_writing_node(core=2)
    selected = {"id": "topic", "title": "当前章节", "goal": Coordinator.WRITING_AXES[0]["goal"],
        "chapter_id": "chapter1"}
    with pytest.raises(BrainstormGenerationError):
        Coordinator._bind_node_candidate(invalid, requested, selected, "writing", Coordinator.DEFAULT_LIMITS,
            writing_axis=Coordinator.WRITING_AXES[0])
    before = copy.deepcopy(invalid)
    coordinator, service = setup([invalid, invalid])
    result = await prepare(coordinator, stage="writing")
    assert result["next_goals"] == [{"option_id": identifier,
        "goals": [Coordinator.WRITING_AXES[0]["levels"][1]]} for identifier in ("a", "b")]
    assert invalid == before and service.model_calls == [] and len(service.responses) == 2


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_writing_decoder_only_generates_question_and_keeps_actual_option_budget(depth):
    axis = Coordinator.WRITING_AXES[0]
    requested = node(depth, 3)
    requested["goal"] = axis["levels"][depth - 1]
    schema = Coordinator._node_schema(requested, {"status": "skipped", "sources": []},
        {**Coordinator.DEFAULT_LIMITS, "max_depth": 3}, writing_axis=axis)
    assert set(schema["properties"]) == {"question"}
    assert schema["required"] == ["question"]
    options = schema["properties"]["question"]["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (2, requested["max_options"])


@pytest.mark.parametrize("axis", Coordinator.WRITING_AXES)
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_writing_compiler_covers_every_actual_option_and_stops_only_at_leaf(axis, depth, count):
    requested = node(depth, 3)
    requested.update(goal=axis["levels"][depth - 1], max_options=5,
        max_next_goals=0 if depth == 3 else 5)
    value = {"question": writing_candidate(depth, 3)["question"]}
    value["question"]["options"] = [{"label": "组织方案{}".format(index),
        "description": "本章独立组织办法{}".format(index), "recommended": index == count - 1,
        "memory_choice": "original"} for index in range(count)]
    selected = {"id": "topic", "title": "当前章节", "goal": axis["goal"], "chapter_id": "chapter1"}
    before = copy.deepcopy((value, requested, selected))
    bound, future = Coordinator._bind_node_candidate(value, requested, selected, "writing",
        {**Coordinator.DEFAULT_LIMITS, "max_depth": 3}, writing_axis=axis)
    actual_ids = [option["id"] for option in bound["questions"][0]["options"]]
    assert actual_ids == [chr(ord("a") + index) for index in range(count)]
    assert future == ([] if depth == 3 else [{"option_id": identifier,
        "goals": [axis["levels"][depth]]} for identifier in actual_ids])
    assert (value, requested, selected) == before


@pytest.mark.asyncio
async def test_writing_question_only_candidate_needs_no_model_owned_branch_array():
    value = {"question": writing_candidate()["question"]}
    coordinator, service = setup([value])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs))
    coordinator._check_stage_consistency = inspect
    result = await prepare(coordinator, stage="writing")
    assert result["next_goals"] == [{"option_id": identifier,
        "goals": [Coordinator.WRITING_AXES[0]["levels"][1]]} for identifier in ("a", "b")]
    assert len(seen) == 1
    task = seen[0]["task"]
    assert task["program_bound_next_goals"] is True
    assert task["program_bound_editorial"] is True
    assert task["selected_chapter"]["id"] == "chapter1"
    assert "交代本章人物和事件" in task["selected_chapter"]["goal"]
    assert seen[0]["result"] == result
    Coordinator._verify_editorial_compilation(result=result, task=task, topic=seen[0]["topic"])
    assert service.model_calls == [] and len(service.responses) == 1


def test_writing_compiler_cannot_cut_valid_branches_to_fit_an_insufficient_budget():
    axis = Coordinator.WRITING_AXES[0]
    requested = {**node(), "goal": axis["levels"][0], "max_next_goals": 1}
    with pytest.raises(BrainstormGenerationError, match="预算"):
        Coordinator._bind_node_candidate({"question": writing_candidate()["question"]}, requested,
            {"id": "topic", "title": "章节", "goal": axis["goal"]}, "writing",
            Coordinator.DEFAULT_LIMITS, writing_axis=axis)


@pytest.mark.parametrize("chapters", [None, [], [{"id": "other", "title": "不同标题", "goal": "不同任务"}],
    [{"id": "chapter1", "title": "本章", "goal": "本章任务"}] * 2,
    [{"id": "chapter1", "title": "本章", "goal": "本章任务", "operation_id": "fake"}]])
@pytest.mark.asyncio
async def test_missing_or_ambiguous_local_chapter_binding_cannot_issue_a_model_request(chapters):
    coordinator, service = setup([])
    with pytest.raises(BrainstormGenerationError, match="章节"):
        await prepare(coordinator, stage="writing", plan={"chapters": chapters})
    assert service.model_calls == []


def test_local_content_is_bound_by_chapter_identity_and_independent_scope_evidence():
    topic = {"id": "topic", "title": "组织能力标题可不同", "goal": Coordinator.WRITING_AXES[0]["goal"],
        "chapter_id": "chapter2"}
    plan = {"chapters": [{"id": "chapter1", "title": "相同表面词", "goal": "先章的独立主任务"},
        {"id": "chapter2", "title": "相同表面词", "goal": "本章的独立主任务"}]}
    before = copy.deepcopy(plan)
    chapter = Coordinator._writing_chapter(topic, plan)
    assert chapter == plan["chapters"][1] and plan == before
    context = {"question": {"prompt": "如何衔接本章内容？", "options": []},
        "effective_goal": Coordinator.WRITING_AXES[0]["levels"][0], "ancestor_conditions": [],
        "selected_chapter": chapter}
    task = {"fixed_decisions": [], "cleared_constraints": []}
    unit = {"kind": "question", "text": context["question"]["prompt"]}
    schema = Coordinator._bound_scope_schema(unit, context, task, {"pass": ("none", [])})
    evidence = Coordinator._chapter_scope_evidence([chapter])["chapter0"]["evidence_ref"]
    assert schema["properties"]["chapter_evidence_ref"]["enum"] == [evidence]
    record = {field: "no" if value["enum"] == ["yes", "no", "unknown"] else value["enum"][0]
        for field, value in schema["properties"].items()}
    assert Coordinator._bound_scope_verdicts({"unit": record}, {"unit": schema}) == {"unit": "pass"}
    record["chapter_evidence_ref"] = Coordinator._chapter_scope_evidence(plan["chapters"])["chapter0"]["evidence_ref"]
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._bound_scope_verdicts({"unit": record}, {"unit": schema})


@pytest.mark.parametrize("mutation", [
    lambda chapter: chapter.update(id="other"),
    lambda chapter: chapter.update(title="同一标识下被替换的标题"),
    lambda chapter: chapter.update(goal="同一标识下被替换的正文任务"),
])
def test_scope_chapter_reference_rejects_stale_full_identity_evidence(mutation):
    chapter = {"id": "chapter1", "title": "局部内容", "goal": "解释本章对象与依据"}
    stale = Coordinator._chapter_scope_evidence([chapter])["chapter0"]["evidence_ref"]
    mutation(chapter)
    context = {"question": {"prompt": "如何组织本章说明段？", "options": []},
        "effective_goal": Coordinator.WRITING_AXES[0]["levels"][0], "ancestor_conditions": [],
        "selected_chapter": chapter}
    schema = Coordinator._bound_scope_schema({"kind": "question", "text": context["question"]["prompt"]},
        context, {"fixed_decisions": [], "cleared_constraints": []}, {"pass": ("none", [])})
    record = {field: "no" if value["enum"] == ["yes", "no", "unknown"] else value["enum"][0]
        for field, value in schema["properties"].items()}
    assert schema["properties"]["chapter_evidence_ref"]["enum"] != [stale]
    record["chapter_evidence_ref"] = stale
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._bound_scope_verdicts({"unit": record}, {"unit": schema})
    record["chapter_evidence_ref"] = "0" * 64
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._bound_scope_verdicts({"unit": record}, {"unit": schema})


def bound_path_case(depth=1, maximum=2):
    axis = Coordinator.WRITING_AXES[0]
    requested = node(depth, maximum)
    requested["goal"] = axis["levels"][depth - 1]
    selected = {"id": "topic", "title": "具体章节", "goal": axis["goal"], "chapter_id": "chapter1"}
    chapter = {"id": "chapter1", "title": "具体章节", "goal": "解释本章对象与依据"}
    raw, future = Coordinator._bind_node_candidate({"question": writing_candidate(depth, maximum)["question"]},
        requested, selected, "writing", {**Coordinator.DEFAULT_LIMITS, "max_depth": maximum}, writing_axis=axis)
    task = {"goal": requested["goal"], "node_goal": requested["goal"], "depth": depth, "max_depth": maximum,
        "selected_topic": selected, "selected_chapter": chapter, "ancestor_conditions": requested["ancestor_conditions"]}
    return {"question": raw["questions"][0], "next_goals": future}, selected, task


@pytest.mark.parametrize("depth,maximum", [(1, 2), (2, 2), (1, 3), (2, 3), (3, 3)])
def test_program_future_proof_binds_actual_parent_option_chapter_and_depth(depth, maximum):
    value, selected, task = bound_path_case(depth, maximum)
    before = copy.deepcopy((value, selected, task))
    paths = Coordinator._validated_writing_future_paths(value, selected, task)
    assert paths == ([] if depth == maximum else [{"parent_question_id": value["question"]["id"],
        "parent_option_id": group["option_id"], "chapter_id": "chapter1", "child_depth": depth + 1,
        "child_goal": Coordinator.WRITING_AXES[0]["levels"][depth]} for group in value["next_goals"]])
    assert (value, selected, task) == before


@pytest.mark.parametrize("change", [
    lambda v, t, s: s.pop("depth"),
    lambda v, t, s: s.update(depth=True),
    lambda v, t, s: s.update(max_depth=1),
    lambda v, t, s: s.update(node_goal="宽泛全文目标"),
    lambda v, t, s: s["selected_chapter"].update(id="wrong"),
    lambda v, t, s: s.update(selected_topic={**t, "id": "wrong"}),
    lambda v, t, s: v["question"]["options"][1].update(id="a"),
    lambda v, t, s: v["next_goals"].pop(),
    lambda v, t, s: v["next_goals"][1].update(option_id="a"),
    lambda v, t, s: v["next_goals"][0].update(goals=["重选已定来源"]),
    lambda v, t, s: v["next_goals"][0].update(goals=v["next_goals"][0]["goals"] * 2),
    lambda v, t, s: v["next_goals"][0].update(extra="fake"),
    lambda v, t, s: v["question"].update(parent_option_id="fake"),
])
def test_boolean_program_tag_cannot_hide_missing_wrong_or_free_paths(change):
    value, selected, task = bound_path_case()
    task["program_bound_next_goals"] = True
    change(value, selected, task)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._validated_writing_future_paths(value, selected, task)


def test_machine_leaf_cannot_hide_an_extra_child_and_ancestors_cannot_be_dropped():
    value, selected, task = bound_path_case(2, 2)
    value["next_goals"] = [{"option_id": "a", "goals": [Coordinator.WRITING_AXES[0]["levels"][2]]}]
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._validated_writing_future_paths(value, selected, task)
    value["next_goals"] = []
    task["ancestor_conditions"] = []
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._validated_writing_future_paths(value, selected, task)


@pytest.mark.parametrize("field", ["parent_question_id", "option_label", "option_description"])
def test_program_paths_require_complete_actual_ancestor_fields(field):
    value, selected, task = bound_path_case(2, 3)
    task["ancestor_conditions"][0].pop(field)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        Coordinator._validated_writing_future_paths(value, selected, task)
