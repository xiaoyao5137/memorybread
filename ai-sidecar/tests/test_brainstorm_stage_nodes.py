"""Bounded single-node preparation, source snapshots and cancellation."""

import asyncio
import copy
import importlib
import json
import sqlite3
import threading
import time

import httpx
import pytest

from creation.brainstorm import BrainstormGenerationError, BrainstormGenerationTimeout
from creation.brainstorm_stages import BrainstormStageCoordinator
from creation.brainstorm_root_axes import load_root_axes_binding
from creation.operations import OperationError
from creation.service import CloudModelRequestError
from tests.brainstorm_axis_fixtures import axis_plan_manifest, axis_plan_raw
from tests.test_brainstorm_stage_citations import MEMORY, NO_MEMORY
from tests.test_brainstorm_stages import topic
from tests.test_creation_brainstorm import StubCreationService
from tests.test_creation_queue import BrainstormDeadlineThreadQueue


CONTEXT_ID = "a" * 64


def node(depth=1, max_depth=2):
    conditions = []
    for index in range(depth - 1):
        conditions.append({"parent_question_id": "parent{}".format(index), "parent_option_id": "a",
            "parent_prompt": "从什么方向展开？", "option_label": ["从人物入手", "采用内心视角"][index],
            "option_description": "保留原分支的具体表达条件"})
    return {"id": "stage-build-1-1", "goal": "比较具体表达切入点", "depth": depth,
        "parent_question_id": conditions[-1]["parent_question_id"] if conditions else None,
        "parent_option_id": "a" if conditions else None, "ancestor_conditions": conditions,
        "max_options": 2, "max_next_goals": 0 if depth == max_depth else 3}


def candidate(depth=1, max_depth=2, choice="original"):
    prompt = ["这部分从何处展开？", "采用什么视角？",
              "如何安排节奏？"][depth - 1]
    return {"question": {"type": "multi_choice", "single_choice_reason": "", "prompt_body": prompt,
        "why_now": "确认当前分支的具体表达取舍", "answer_template": "补充你的想法",
        "options": [{"label": "从人物入手", "description": "通过人物行动展开表达",
            "recommended": True, "memory_choice": choice},
            {"label": "从场景入手", "description": "通过环境细节呈现主题", "recommended": False,
             "memory_choice": "original"}]}, "next_goals": [] if depth == max_depth else [
                 {"option_index": 1, "goal": "比較人物视角"}, {"option_index": 1, "goal": "安排人物出场"},
                 {"option_index": 2, "goal": "比较场景节奏"}]}


def set_goals(value, option_index, goals):
    value["next_goals"] = [item for item in value["next_goals"] if item["option_index"] != option_index] + [
        {"option_index": option_index, "goal": goal} for goal in goals]


def provider_candidate(value):
    """Use the new provider wire shape for valid flat legacy test fixtures.

    Direct binder tests keep their canonical flat contract; malformed indices
    or mixed protocols are never silently dropped or repaired by this adapter.
    """
    result = copy.deepcopy(value)
    if (not isinstance(result, dict) or set(result) != {"question", "next_goals"}
            or not isinstance(result.get("question"), dict)
            or "prompt_body" not in result["question"]
            or not isinstance(result["question"].get("options"), list)
            or not isinstance(result.get("next_goals"), list)):
        return result
    options = result["question"]["options"]
    if any(not isinstance(option, dict) or "next_goals" in option for option in options):
        return result
    if any(not isinstance(item, dict) or set(item) != {"option_index", "goal"}
            or type(item["option_index"]) is not int or not 1 <= item["option_index"] <= len(options)
            for item in result["next_goals"]):
        return result
    for option in options:
        option["next_goals"] = []
    for item in result.pop("next_goals"):
        options[item["option_index"] - 1]["next_goals"].append(item["goal"])
    return result


def setup(responses, memory=NO_MEMORY, *, check_consistency=False):
    service = StubCreationService([json.dumps(provider_candidate(value), ensure_ascii=False) for value in responses])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(memory if args[-1] else NO_MEMORY)
    # Relationship certificates have a separate protocol suite. These fixtures
    # exercise the existing per-unit/fixed-rule/citation gates in isolation.
    async def relationship_pass(**kwargs):
        return None
    coordinator._check_new_decision_space = relationship_pass
    if not check_consistency:
        # Structural/citation fixtures isolate their gate from the separately
        # exercised independent consistency protocol below.
        async def consistency_pass(**kwargs):
            return None
        coordinator._check_stage_consistency = consistency_pass
        coordinator._check_plan_consistency = consistency_pass
    return coordinator, service


async def prepare(coordinator, *, stage="detail", **kwargs):
    selected_topic = topic()
    if stage == "writing":
        selected_topic["chapter_id"] = "chapter1"
        selected_topic["goal"] = BrainstormStageCoordinator.WRITING_AXES[0]["goal"]
    request = {"stage": stage, "kind": "node", "root_request": "构思人物故事", "decisions": [],
        "topic": selected_topic, "context_id": CONTEXT_ID, "node": node()}
    if stage == "writing":
        request["node"]["goal"] = BrainstormStageCoordinator.WRITING_AXES[0]["levels"][0]
        request["plan"] = {"chapters": [{"id": "chapter1", "title": "人物与事件说明",
            "goal": "交代本章人物和事件，不承担其余章节主任务"}]}
    request.update(kwargs)
    if request["kind"] == "topic_plan" and stage in ("direction", "detail"):
        request.setdefault("root_axes_binding", load_root_axes_binding())
        if "topic" not in kwargs:
            manifest = axis_plan_manifest(stage, limits=request.get("limits"))
            request["topic"] = manifest["topics"][0]
            request.setdefault("plan", {"topics": manifest["topics"]})
    return await coordinator.stage_batch(**request)


@pytest.mark.parametrize("failure", [
    CloudModelRequestError(503, "private provider response"),
    OperationError("MODEL_RATE_LIMITED", "private provider response"),
    httpx.ReadTimeout("private provider response"),
])
@pytest.mark.parametrize("first_invalid", [False, True])
@pytest.mark.asyncio
async def test_node_transient_exhaustion_preserves_provider_failure_category(
    failure, first_invalid, caplog,
):
    coordinator, _service = setup([], NO_MEMORY)
    coordinator.TRANSIENT_RETRY_DELAY_SECONDS = 0
    calls = []

    async def fail_complete(*_args, **_kwargs):
        calls.append(1)
        if first_invalid and len(calls) == 1:
            raise BrainstormGenerationError("候选格式无效")
        raise failure

    coordinator._complete = fail_complete
    with pytest.raises(type(failure)) as caught:
        await prepare(coordinator)

    assert caught.value is failure
    assert len(calls) == coordinator.MAX_NODE_GENERATION_ATTEMPTS
    assert "private provider response" not in caplog.text


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_three_phases_return_only_bound_current_node_and_multiple_goals(stage):
    value = candidate(choice="reference:m1+m2")
    if stage == "writing":
        value["question"]["options"][0]["memory_choice"] = "original"
        del value["next_goals"]
        for field in ("single_choice_reason", "answer_template"):
            del value["question"][field]
    value["question"]["options"][0]["recommended"] = False
    value["question"]["options"][1]["recommended"] = True
    coordinator, service = setup([value], MEMORY)
    result = await prepare(coordinator, stage=stage)
    assert set(result) == {"question", "next_goals", "memory_brief"}
    question = result["question"]
    assert question["id"] == node()["id"]
    assert question["parent_question_id"] is None
    assert question["dimension_id"] == "topic"
    if stage == "writing":
        assert [option["id"] for option in question["options"]] == ["a", "b"]
        assert [option["label"] for option in question["options"]] == ["先概括再解释", "先解释再归纳"]
        assert result["next_goals"] == [{"option_id": identifier,
            "goals": [BrainstormStageCoordinator.WRITING_AXES[0]["levels"][1]]} for identifier in ("a", "b")]
        assert service.model_calls == [] and len(service.responses) == 1
        assert question["type"] == "single_choice"
        assert question["answer_template"] == "可补充你的具体组织方式。"
        return
    assert [option["id"] for option in question["options"]] == ["b", "a"]
    expected = ([{"option_id": option, "goals": [BrainstormStageCoordinator.WRITING_AXES[0]["levels"][1]]}
        for option in ("a", "b")] if stage == "writing" else
        [{"option_id": "a", "goals": ["比較人物视角", "安排人物出场"]},
         {"option_id": "b", "goals": ["比较场景节奏"]}])
    assert result["next_goals"] == expected
    if stage != "writing":
        assert MEMORY["sources"][0]["content"] in question["options"][1]["details"]
        assert MEMORY["sources"][1]["content"] in question["options"][1]["details"]
    assert len(service.model_calls) == 1 and service.model_calls[0]["num_predict"] == 2400
    model_schema = service.model_calls[0]["json_schema"]["properties"]["question"]["properties"]
    assert not {"id", "parent_question_id", "dimension_id"}.intersection(model_schema)
    option_schema = model_schema["options"]["items"]["properties"]
    assert "id" not in option_schema and "memory_ids" not in option_schema
    assert (option_schema["memory_choice"]["enum"] == ["original"] if stage == "writing" else
        "reference:m1+m2" in option_schema["memory_choice"]["enum"])
    context = json.loads(service.prompts[0])
    assert list(context)[-1] == "generation_task"
    assert context["generation_task"]["node_goal"] == (BrainstormStageCoordinator.WRITING_AXES[0]["levels"][0]
        if stage == "writing" else node()["goal"])


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_topic_plan_supports_several_independent_roots_with_complete_budget(stage):
    raw = {"root_axis_ids": (["information_granularity", "evidence_presentation"]
        if stage == "detail" else ["narrative_perspective", "content_focus"])}
    coordinator, service = setup([raw])
    kwargs = {}
    if stage != "writing":
        budget = {"max_topics": 1}
        manifest = axis_plan_manifest(stage, raw["root_axis_ids"], budget)
        kwargs = {"topic": manifest["topics"][0], "plan": {"topics": manifest["topics"]}, "limits": budget}
    result = await prepare(coordinator, stage=stage, kind="topic_plan", node=None, root_limit=2, **kwargs)
    if stage == "writing":
        assert result == {"root_goals": [BrainstormStageCoordinator.WRITING_AXES[0]["levels"][0]]}
        assert service.model_calls == []
        return
    configured = {axis["id"]: axis for axis in load_root_axes_binding()["config"]["axes"]}
    assert result == {"root_goals": [configured[identifier]["goal"] for identifier in raw["root_axis_ids"]]}
    assert service.model_calls == []


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.asyncio
async def test_children_keep_every_true_ancestor_condition_without_extra_copy(depth):
    coordinator, service = setup([candidate(depth, 3)])
    result = await prepare(coordinator, node=node(depth, 3), limits={"max_depth": 3})
    assert result["question"]["parent_question_id"] == "parent{}".format(depth - 2)
    for condition in node(depth, 3)["ancestor_conditions"]:
        assert condition["option_label"] in result["question"]["prompt"]
    assert len(service.model_calls) == 1
    assert bool(result["next_goals"]) is (depth != 3)


@pytest.mark.parametrize("repeated", ["制定预算核算规则", " 制定 预算核算规则！？ "])
@pytest.mark.asyncio
async def test_child_goal_repeating_trusted_current_goal_rejects_whole_candidate(repeated):
    requested = {**node(), "goal": "制定预算核算规则"}
    value = candidate()
    set_goals(value, 1, [repeated])
    original = copy.deepcopy(value)
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError, match="当前节点"):
        await prepare(coordinator, node=requested)
    assert len(service.model_calls) == 2
    assert value == original
    feedback = json.loads(service.prompts[1])["validation_failures"]
    assert any("具体子决定" in item for item in feedback)


@pytest.mark.asyncio
async def test_repeated_parent_goal_retries_current_candidate_without_trimming_branches():
    requested = {**node(), "goal": "制定预算核算规则"}
    bad = candidate()
    set_goals(bad, 1, [requested["goal"]])
    good = candidate()
    set_goals(good, 1, ["确认支出字段与科目映射方式", "验证金额舍入边界"])
    set_goals(good, 2, ["比较跨表核对流程"])
    coordinator, service = setup([bad, good])
    result = await prepare(coordinator, node=requested)
    assert len(service.model_calls) == 2
    assert result["next_goals"] == [
        {"option_id": "a", "goals": ["确认支出字段与科目映射方式", "验证金额舍入边界"]},
        {"option_id": "b", "goals": ["比较跨表核对流程"]}]


@pytest.mark.asyncio
async def test_child_goal_may_not_reopen_explicit_trusted_ancestor_goal():
    requested = node(2, 3)
    requested["ancestor_conditions"][0]["parent_goal"] = "制定预算核算规则"
    value = candidate(2, 3)
    set_goals(value, 1, ["制定 预算核算规则。"])
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError, match="当前节点"):
        await prepare(coordinator, node=requested, limits={"max_depth": 3})
    assert len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_narrower_child_goal_passes_without_inferring_goal_from_display_prompt():
    requested = node(2, 3)
    requested["ancestor_conditions"][0]["parent_prompt"] = "比较候选叙事节奏"
    value = candidate(2, 3)
    set_goals(value, 1, ["比较候选叙事节奏"])
    set_goals(value, 2, ["安排场景转换节点"])
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested, limits={"max_depth": 3})
    assert len(service.model_calls) == 1
    assert result["next_goals"] == [{"option_id": "a", "goals": ["比较候选叙事节奏"]},
        {"option_id": "b", "goals": ["安排场景转换节点"]}]


@pytest.mark.parametrize("mutation", [
    lambda value: value["question"].update(id="injected"),
    lambda value: value["question"]["options"][0].update(id="injected"),
    lambda value: set_goals(value, 1, []),
    lambda value: set_goals(value, 1, ["同一目标", "同一目标"]),
    lambda value: set_goals(value, 1, ["目标一", "目标二", "目标三"]),
    lambda value: value["question"]["options"][0].update(memory_choice="reference:forged"),
    lambda value: value["question"]["options"][0].update(memory_mode="reference", memory_ids=["m1"]),
])
@pytest.mark.asyncio
async def test_bad_node_structure_or_sources_retry_only_current_node_twice(mutation):
    value = candidate()
    mutation(value)
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError, match="当前节点"):
        await prepare(coordinator)
    assert len(service.model_calls) == 2
    assert all(call["num_predict"] == 2400 for call in service.model_calls)


@pytest.mark.asyncio
async def test_leaf_cannot_hide_new_branches_or_rewrite_parent_metadata():
    value = candidate(2)
    value["next_goals"] = [{"option_index": 1, "goal": "偷偷增加分支"}]
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, node=node(2))
    assert len(service.model_calls) == 2


@pytest.mark.parametrize("kwargs", [
    {"context_id": "bad"}, {"node": {**node(), "depth": 2}},
    {"node": {**node(), "max_next_goals": 1}},
    {"node": {**node(), "goal": "长" * 81}},
    {"kind": "topic_plan", "root_limit": 13},
    {"kind": "topic_plan", "root_limit": 0, "limits": {"max_depth": 3}},
])
@pytest.mark.asyncio
async def test_invalid_trusted_identity_or_frontier_budget_has_no_recall_or_model(kwargs):
    coordinator, service = setup([])
    coordinator._retrieve_memory = lambda *args: pytest.fail("invalid metadata must not retrieve")
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, **kwargs)
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_saved_prompt_duplicate_regenerates_current_root_without_altering_prefix():
    duplicate = candidate()
    good = copy.deepcopy(duplicate)
    good["question"]["prompt_body"] = "当前主题采用哪种画面结构？"
    previous = [duplicate["question"]["prompt_body"]]
    coordinator, service = setup([duplicate, good])
    result = await prepare(coordinator, plan={"prepared_question_prompts": previous})
    assert result["question"]["prompt"] == good["question"]["prompt_body"]
    assert previous == [duplicate["question"]["prompt_body"]]
    assert len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_child_copy_collision_retries_small_copy_and_keeps_parent_sources():
    value = candidate(2, choice="reference:m1+m2")
    value["question"]["prompt_body"] = "需要进一步比较如何保留人物心态与人物行为的对应关系？" * 3
    previous = ["若「从人物入手」，采用什么视角？"]
    coordinator, service = setup([value,
        {"replacements": {"q0_prompt": "采用什么视角？"}},
        {"replacements": {"q0_prompt": "如何安排对白节奏？"}}], MEMORY)
    result = await prepare(coordinator, node=node(2), plan={"prepared_question_prompts": previous})
    assert result["question"]["prompt"] == "若「从人物入手」，如何安排对白节奏？"
    assert result["question"]["parent_question_id"] == "parent0"
    assert result["next_goals"] == []
    assert MEMORY["sources"][1]["content"] in result["question"]["options"][0]["details"]
    assert len(service.model_calls) == 3
    assert "replacements" in service.model_calls[1]["json_schema"]["properties"]
    assert "replacements" in service.model_calls[2]["json_schema"]["properties"]


@pytest.mark.asyncio
async def test_revoked_permission_cannot_reference_even_when_topic_claims_permission():
    value = candidate(choice="reference:m1")
    coordinator, service = setup([value, value], MEMORY)
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, root_request="只用当前输入，不检索个人资料",
            topic={**topic(), "goal": "已允许检索历史"})
    assert all(json.loads(prompt)["memory"]["status"] == "skipped" for prompt in service.prompts)
    option = service.model_calls[0]["json_schema"]["properties"]["question"]["properties"]["options"]["items"]
    assert option["properties"]["memory_choice"]["enum"] == ["original"]


def test_compact_context_retains_all_user_words_and_clear_exclusion_metadata():
    root = "请保留全部原始条件、排除范围和用户的具体决定，不要丢失末尾文字。"
    decisions = [{"question_id": "q", "question": "完整原题文字与附加约束应原样保留在唯一确认上下文中",
        "answer": "用户确认的完整长答案包含年份2026和GPU指标，不得静默截断",
        "answer_source": "user", "selected_options": [{"label": "GPU利用率", "tradeoff": "不讨论成本"}],
        "user_inputs": ["保留详细用户补充限制与指标口径"], "manually_edited": True},
        {"answer_source": "user_excluded", "excluded_topic": "商业推广", "answer": "不展开商业推广"}]
    edits = {"open_flags": "", "q": "已明确修订的条件不可从旧值恢复"}
    brief = root + "\n" + decisions[0]["question"] + "\n" + decisions[0]["answer"] + "\n保留未匹配人工文字"
    context = BrainstormStageCoordinator._node_model_context(root, decisions, brief, [], edits)
    assert context["root_request"] == root and context["confirmed_context"] == decisions
    assert context["brief_edits"] == edits
    assert "保留未匹配人工文字" in context["brief_context"]
    assert "⟦root_request⟧" in context["brief_context"]
    assert "⟦confirmed_context[0].answer⟧" in context["brief_context"]


def cache_setup(tmp_path):
    path = tmp_path / "sources.sqlite"
    memory = copy.deepcopy(MEMORY)
    memory["sources"] = memory["sources"][:2]
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE bake_documents(id INTEGER PRIMARY KEY,title TEXT,summary TEXT,full_content TEXT,updated_at INTEGER,deleted_at INTEGER)")
        conn.execute("CREATE TABLE bake_document_source_heads(document_id INTEGER PRIMARY KEY,snapshot_id INTEGER,applied_at INTEGER)")
        conn.execute("CREATE TABLE bake_document_source_snapshots(id INTEGER PRIMARY KEY,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT)")
        conn.execute("CREATE TABLE creation_checkpoints(id INTEGER PRIMARY KEY,payload TEXT)")
        for source in memory["sources"]:
            conn.execute("INSERT INTO bake_documents VALUES(?,?,?,?,?,NULL)",
                (source["source_id"],source["title"],source["content"],source["content"],source["updated_at"]))
            conn.execute("INSERT INTO bake_document_source_heads VALUES(?,?,1)", (source["source_id"],source["source_id"]))
            conn.execute("INSERT INTO bake_document_source_snapshots VALUES(?,?,?,1,'complete')",
                (source["source_id"],source["source_id"],source["content"]))
    coordinator, service = setup([candidate() for _ in range(16)], memory)
    service.db_path = str(path)
    calls = []
    def retrieve(*args):
        calls.append(args)
        return copy.deepcopy(memory if args[-1] else NO_MEMORY)
    coordinator._retrieve_memory = retrieve
    return coordinator, service, path, memory, calls


@pytest.mark.asyncio
async def test_source_cache_survives_checkpoint_and_derived_brief_growth(tmp_path, caplog):
    caplog.set_level("INFO", logger="creation.brainstorm_stages")
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator, brief_markdown="既有确认上下文")
    with sqlite3.connect(str(path)) as conn:
        conn.execute("INSERT INTO creation_checkpoints VALUES(1,'checkpoint')")
    await prepare(coordinator, node={**node(), "id": "next-node"},
        brief_markdown="既有确认上下文\n## 历史记忆参考\n新增来源片段",
        plan={"prepared_question_prompts": ["已保存的另一题？"]})
    assert len(calls) == 1
    assert "hit=True" in caplog.text


@pytest.mark.parametrize("changes", [
    {"context_id": "b" * 64}, {"root_request": "增加真实用户限制"},
    {"decisions": [{"answer": "更新了真实确认答案", "answer_source": "user"}]},
    {"brief_edits": {"open_flags": "新增用户修订"}},
    {"user_input_revisions": {"q": 2}}, {"selected_skills": [{"id": "new", "title": "新能力"}]},
])
@pytest.mark.asyncio
async def test_source_cache_bound_to_complete_raw_confirmed_inputs(tmp_path, changes):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator)
    await prepare(coordinator, **changes)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_selected_body_head_deletion_and_ttl_each_invalidate_cache(tmp_path):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE bake_documents SET full_content=full_content||'真实追加内容' WHERE id=1")
        conn.execute("UPDATE bake_document_source_snapshots SET content_text=content_text||'真实追加内容' WHERE id=1")
    await prepare(coordinator)
    assert len(calls) == 2
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE bake_document_source_snapshots SET content_text='不同的原始源正文' WHERE id=1")
    await prepare(coordinator)
    assert len(calls) == 3
    assert coordinator._source_version(memory) is None
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE bake_documents SET deleted_at=1 WHERE id=1")
    await prepare(coordinator)
    assert len(calls) == 4
    coordinator.MEMORY_CACHE_TTL_SECONDS = 0
    await prepare(coordinator)
    assert len(calls) == 5


@pytest.mark.parametrize("mutation", ["body", "title"])
@pytest.mark.asyncio
async def test_source_changes_during_recall_are_not_registered_as_current(tmp_path, mutation):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    def retrieve(*args):
        calls.append(args)
        with sqlite3.connect(str(path)) as conn:
            if mutation == "body":
                conn.execute("UPDATE bake_documents SET full_content='changed body' WHERE id=1")
                conn.execute("UPDATE bake_document_source_snapshots SET content_text='changed body' WHERE id=1")
            else:
                conn.execute("UPDATE bake_documents SET title='changed title' WHERE id=1")
        return copy.deepcopy(memory)
    coordinator._retrieve_memory = retrieve
    await prepare(coordinator)
    await prepare(coordinator)
    assert len(calls) == 2 and not coordinator._memory_cache


@pytest.mark.asyncio
async def test_cache_ttl_is_not_extended_by_hits_and_new_sources_appear_after_expiry(tmp_path):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator)
    entry_before = list(coordinator._memory_cache.values())[0][0]
    with sqlite3.connect(str(path)) as conn:
        conn.execute("INSERT INTO bake_documents VALUES(3,'new','new','new',1,NULL)")
    await prepare(coordinator)
    assert len(calls) == 1
    assert list(coordinator._memory_cache.values())[0][0] == entry_before
    coordinator.MEMORY_CACHE_TTL_SECONDS = 0
    await prepare(coordinator)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_revoking_source_permission_skips_existing_cached_snapshot(tmp_path):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator)
    await prepare(coordinator, root_request="只用当前输入，不检索个人资料")
    assert calls[-1][-1] is False
    assert json.loads(service.prompts[-1])["memory"]["sources"] == []


@pytest.mark.asyncio
async def test_retrieval_timeout_releases_caller_without_waiting_background_thread(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    coordinator, service = setup([])
    def slow(*args):
        started.set()
        release.wait(2)
        return copy.deepcopy(NO_MEMORY)
    coordinator._retrieve_memory = slow
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.04)
    before = time.monotonic()
    try:
        with pytest.raises(BrainstormGenerationTimeout):
            await prepare(coordinator)
        assert started.is_set() and not release.is_set()
        assert time.monotonic() - before < 0.5
        assert service.model_calls == []
    finally:
        release.set()


@pytest.mark.asyncio
async def test_node_endpoint_preserves_new_metadata_and_uses_existing_queue(monkeypatch):
    app = importlib.import_module("creation.app")
    captured = []
    async def stage_batch(**kwargs):
        captured.append(kwargs)
        return {"question": {"id": node()["id"]}, "next_goals": []}
    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json={"root_request": "短篇",
            "stage": "detail", "kind": "node", "topic": topic(), "context_id": CONTEXT_ID, "node": node()})
    assert response.status_code == 200
    assert captured[0]["node"] == node() and captured[0]["context_id"] == CONTEXT_ID
    assert captured[0]["kind"] == "node"
    await asyncio.to_thread(queue.worker.join, 1)
    assert not queue.worker.is_alive()


def test_nonwriting_plan_schema_prevents_chapter_id_retry():
    schema = BrainstormStageCoordinator._stage_schema("plan", "detail",
        BrainstormStageCoordinator.DEFAULT_LIMITS, NO_MEMORY)
    chapter = schema["properties"]["topics"]["items"]["properties"]["chapter_id"]
    assert chapter == {"type": "string", "enum": [""]}


def decoder_array_budget_accepts(value, schema):
    """Simulate decoder structural bounds on an actual candidate document.

    This checks the returned schema, including after shared compaction; it is
    independent of the coordinator's business validator and cannot trim input.
    """
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        return False
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(schema["properties"]):
            return False
        return all(key in value and decoder_array_budget_accepts(value[key], child)
                   for key, child in schema["properties"].items())
    if schema.get("type") == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            return False
        return all(decoder_array_budget_accepts(item, schema["items"]) for item in value)
    return True


def minimal_node_candidate(options=2, goals_per_option=1):
    value = candidate()
    value["question"]["options"] = []
    value["next_goals"] = []
    for index in range(options):
        value["question"]["options"].append({"label": "不同方向{}".format(index),
            "description": "独立方法及对应取舍{}".format(index), "recommended": index == 0,
            "memory_choice": "original"})
        value["next_goals"].extend({"option_index": index + 1, "goal": "具体决定{}-{}".format(index, goal)}
            for goal in range(goals_per_option))
    return value


@pytest.mark.parametrize("max_options", [2, 3, 4, 5])
def test_node_decoder_really_bounds_option_count_after_shared_compaction(max_options):
    requested = {**node(), "max_options": max_options, "max_next_goals": max_options}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(max_options)), schema)
    for illegal_count in (1, max_options + 1):
        value = provider_candidate(minimal_node_candidate(illegal_count))
        before = copy.deepcopy(value)
        assert not decoder_array_budget_accepts(value, schema)
        assert value == before


def test_tight_two_goal_decoder_prevents_both_live_mechanical_budget_failures():
    requested = {**node(), "max_next_goals": 2}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    valid = minimal_node_candidate(2, 1)
    assert decoder_array_budget_accepts(provider_candidate(valid), schema)
    BrainstormStageCoordinator._bind_node_candidate(valid, requested, topic(), "detail",
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    for options, goals in ((3, 1), (2, 0)):
        assert not decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(options, goals)), schema)
    excess = provider_candidate(minimal_node_candidate(2, 2))
    assert not decoder_array_budget_accepts(excess, schema)
    with pytest.raises(BrainstormGenerationError, match="总数.*max_next_goals=2"):
        BrainstormStageCoordinator._compile_provider_node_candidate(excess, requested,
            BrainstormStageCoordinator.DEFAULT_LIMITS)


def test_grouped_node_decoder_covers_each_branch_and_program_enforces_asymmetric_whole_sum():
    requested = {**node(), "max_options": 3, "max_next_goals": 4}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    allowed = minimal_node_candidate(2, 1)
    set_goals(allowed, 1, ["独立人物决定{}".format(index) for index in range(3)])
    assert decoder_array_budget_accepts(provider_candidate(allowed), schema)
    bound, groups = BrainstormStageCoordinator._bind_node_candidate(allowed, requested, topic(), "detail",
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert sum(len(group["goals"]) for group in groups) == 4
    oversized = copy.deepcopy(allowed)
    oversized["next_goals"].append({"option_index": 2, "goal": "第二个独立场景决定"})
    assert decoder_array_budget_accepts(provider_candidate(oversized), schema)
    total_oversized = minimal_node_candidate(3, 2)
    assert decoder_array_budget_accepts(provider_candidate(total_oversized), schema)
    with pytest.raises(BrainstormGenerationError, match="不能删分支"):
        BrainstormStageCoordinator._compile_provider_node_candidate(provider_candidate(total_oversized), requested,
            BrainstormStageCoordinator.DEFAULT_LIMITS)


def test_leaf_decoder_allows_no_goals_and_keeps_bound_option_count():
    requested = node(2)
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(2, 0)), schema)
    assert not decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(2, 1)), schema)
    assert not decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(3, 0)), schema)
    option = schema["properties"]["question"]["properties"]["options"]["items"]["properties"]
    assert "maxLength" not in option["label"]


@pytest.mark.parametrize("budget", [3, 23])
def test_asymmetric_legal_goal_allocation_is_preserved_at_small_and_large_boundaries(budget):
    requested = {**node(), "max_next_goals": budget}
    limits = {**BrainstormStageCoordinator.DEFAULT_LIMITS, "max_questions_per_topic": 24}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY, limits)
    value = minimal_node_candidate(2, 1)
    set_goals(value, 1, ["独立人物决定{}".format(index) for index in range(budget - 1)])
    before = copy.deepcopy(value)
    assert decoder_array_budget_accepts(provider_candidate(value), schema)
    _, groups = BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "detail", limits)
    assert value == before
    assert len(groups[0]["goals"]) == budget - 1 and len(groups[1]["goals"]) == 1


@pytest.mark.asyncio
async def test_child_condition_preserves_spaces_and_punctuation_like_core():
    requested = node(2)
    requested["ancestor_conditions"][0]["option_label"] = "方案 A（GPU）"
    value = candidate(2)
    value["question"]["prompt_body"] = "如何安排节奏？"
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested)
    assert "方案 A（GPU）" in result["question"]["prompt"]
    assert result["question"]["prompt"] == "若「方案 A（GPU）」，如何安排节奏？"
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_document_observed_at_projection_allows_cache_hit(tmp_path):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    for source in memory["sources"]:
        source["observed_at"] = source["updated_at"]
    await prepare(coordinator)
    await prepare(coordinator)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["root", "next"])
@pytest.mark.asyncio
async def test_punctuation_only_goals_are_rejected_before_core(kind):
    value = {"root_goals": ["！！？"]} if kind == "root" else candidate()
    if kind == "next":
        set_goals(value, 1, ["！！？"])
    coordinator, service = setup([value, value])
    kwargs = {}
    if kind == "root":
        manifest = axis_plan_manifest("detail")
        invalid_topic = {**manifest["topics"][0], "goal": "！！？"}
        kwargs = {"kind": "topic_plan", "root_limit": 1, "topic": invalid_topic,
            "plan": {"topics": manifest["topics"]}}
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, **kwargs)
    assert len(service.model_calls) == (0 if kind == "root" else 2)


@pytest.mark.parametrize("options,goals,expected", [(2, 3, 2400), (5, 5, 2600), (5, 23, 4000), (5, 0, 2400)])
def test_node_output_budget_is_structural_and_bounded(options, goals, expected):
    assert BrainstormStageCoordinator._node_output_tokens({"max_options": options, "max_next_goals": goals}) == expected


@pytest.mark.asyncio
async def test_broad_legal_node_keeps_all_goals_with_same_bounded_retry_envelope():
    requested = {**node(), "max_options": 5, "max_next_goals": 23}
    value = candidate()
    set_goals(value, 1, ["独立人物决定{}".format(index) for index in range(12)])
    set_goals(value, 2, ["独立场景决定{}".format(index) for index in range(11)])
    invalid = copy.deepcopy(value)
    invalid["question"]["options"][0]["memory_choice"] = "reference:invalid"
    coordinator, service = setup([invalid, value])
    result = await prepare(coordinator, node=requested, limits={"max_questions_per_topic": 24})
    assert sum(len(group["goals"]) for group in result["next_goals"]) == 23
    assert [call["num_predict"] for call in service.model_calls] == [4000, 4000]


@pytest.mark.parametrize("source_type", ["knowledge", "operation", "data", "pending_document"])
@pytest.mark.asyncio
async def test_all_source_types_refuse_old_excerpt_registered_after_body_changes(tmp_path, source_type):
    path = tmp_path / "selected.sqlite"
    text = "真实的来源正文A" * 30
    title = "当前来源"
    with sqlite3.connect(str(path)) as conn:
        if source_type in {"knowledge", "operation"}:
            table = "bake_knowledge" if source_type == "knowledge" else "bake_sops"
            conn.execute("CREATE TABLE {}(id INTEGER PRIMARY KEY,title TEXT,content TEXT,detailed_content TEXT,summary TEXT,updated_at_ms INTEGER)".format(table))
            conn.execute("INSERT INTO {} VALUES(1,?,?,?,'',100)".format(table), (title,text,""))
            update = "UPDATE {} SET content='不同的来源正文B' WHERE id=1".format(table)
        elif source_type == "data":
            conn.execute("CREATE TABLE data_sources(id INTEGER PRIMARY KEY,title TEXT,status TEXT,deleted_at INTEGER)")
            conn.execute("CREATE TABLE data_snapshots(id INTEGER PRIMARY KEY,source_id INTEGER,content_text TEXT,structured_data TEXT,collected_at INTEGER,observed_at INTEGER)")
            conn.execute("INSERT INTO data_sources VALUES(1,?,'active',NULL)", (title,))
            conn.execute("INSERT INTO data_snapshots VALUES(1,1,?,'',100,100)", (text,))
            update = "UPDATE data_snapshots SET content_text='不同的来源正文B' WHERE id=1"
        else:
            conn.execute("CREATE TABLE captures(id INTEGER PRIMARY KEY,ts INTEGER,url TEXT,is_sensitive INTEGER,webpage_title TEXT,win_title TEXT,ax_text TEXT,ocr_text TEXT,input_text TEXT,audio_text TEXT)")
            conn.execute("INSERT INTO captures VALUES(1,100,'https://example.test',0,?,'',?,'','','')", (title,text))
            update = "UPDATE captures SET ax_text='不同的来源正文B' WHERE id=1"
    memory = {"status": "matched", "sources": [{"memory_id": "m1", "source_type": source_type,
        "source_id": 1, "title": title, "updated_at": 100, "observed_at": 100, "content": text}]}
    coordinator, service = setup([])
    service.db_path = str(path)
    assert coordinator._source_version(memory)
    calls = []
    def retrieve(*args):
        calls.append(args)
        with sqlite3.connect(str(path)) as conn:
            conn.execute(update)
        return copy.deepcopy(memory)
    coordinator._retrieve_memory = retrieve
    raw = {"context_id": CONTEXT_ID}
    await coordinator._stage_memory(raw, "原用户需求", [], "", True)
    await coordinator._stage_memory(raw, "原用户需求", [], "", True)
    assert len(calls) == 2 and not coordinator._memory_cache


@pytest.mark.asyncio
async def test_concurrent_cache_eviction_during_source_verification_is_a_safe_miss(tmp_path):
    coordinator, service, path, memory, calls = cache_setup(tmp_path)
    await prepare(coordinator)
    source_version = coordinator._source_version
    def verify_and_evict(value):
        version = source_version(value)
        with coordinator._memory_cache_lock:
            coordinator._memory_cache.clear()
        return version
    coordinator._source_version = verify_and_evict
    await prepare(coordinator)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_node_copy_uses_same_deadline_and_cancels_pending_repair(monkeypatch):
    closed = asyncio.Event()
    value = candidate(2)
    value["question"]["prompt_body"] *= 8
    class SlowCopy(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            if len(self.model_calls) == 1:
                yield json.dumps(provider_candidate(value), ensure_ascii=False)
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = SlowCopy([])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(NO_MEMORY)
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.04)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, node=node(2))
    assert closed.is_set() and len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_stage_plan_uses_complete_compact_confirmation_context_without_tail_truncation():
    from tests.test_brainstorm_stages import plan
    root = "完整的用户具体要求必须保留所有已确认内容和修订条件。"
    decisions = [{"question": "用户确认问题", "answer": "完整回答", "answer_source": "user"}]
    unshared = "独立人工文字" * 2500
    coordinator, service = setup([axis_plan_raw("detail")])
    await prepare(coordinator, kind="plan", root_request=root, decisions=decisions, brief_markdown=root+unshared)
    context = json.loads(service.prompts[0])
    assert "brief_markdown" not in context
    assert context["root_request"] == root and context["confirmed_context"] == decisions
    assert context["brief_context"] == "⟦root_request⟧" + unshared


@pytest.mark.parametrize("kind", ["topic_plan", "node"])
@pytest.mark.asyncio
async def test_new_prompt_builders_focus_complete_topic_in_last_task_without_reopening_confirmed_scope(kind):
    selected = {"id": "charts", "title": "库存趋势可视化", "goal": "绘制每日库存趋势、缺失标记与图例"}
    elsewhere = {"id": "risk", "title": "风险指标", "goal": "跨主题风险预警不能混入图表配置"}
    decisions = [{"question_id": "q{}".format(index), "question": "已确认的问题{}".format(index),
        "answer": "已确认的完整答案{}".format(index), "answer_source": "user"} for index in range(14)]
    if kind == "topic_plan":
        manifest = axis_plan_manifest("detail", ["evidence_presentation", "information_granularity"])
        selected, elsewhere = manifest["topics"]
        coordinator, service = setup([])
        seen = []
        async def inspect(**kwargs):
            seen.append(copy.deepcopy(kwargs))
        coordinator._check_stage_consistency = inspect
    else:
        coordinator, service = setup([candidate()])
    await prepare(coordinator, kind=kind, topic=selected, decisions=decisions, root_limit=2,
        plan={"topics": [selected, elsewhere]}, node={**node(), "goal": "选择缺失点标记样式"})
    if kind == "topic_plan":
        assert service.model_calls == [] and len(seen) == 1
        task = seen[0]["task"]
        assert task["selected_topic"] == selected and task["goal"] == selected["goal"]
        assert task["program_bound_root_axes"]["topics"] == [selected, elsewhere]
        assert len(task["fixed_decisions"]) == len(decisions)
        assert seen[0]["other_topics"] == [{"id": elsewhere["id"], "title": elsewhere["title"]}]
        assert task["user_constraints"]["root_request"] == "构思人物故事"
        system = BrainstormStageCoordinator._node_scope_prompt(kind)
        assert "禁止从整阶段首题、创作总览或全文任务重新开始" in system
        assert "不得将它们改写成新的root_goals或再次提问" in system
        assert "不能解除原始输入中的检索限制" in system
        return
    context = json.loads(service.prompts[0])
    assert list(context)[-1] == "generation_task"
    task = context["generation_task"]
    assert task["selected_topic"] == selected
    assert task["goal" if kind == "topic_plan" else "node_goal"] == (selected["goal"] if kind == "topic_plan" else "选择缺失点标记样式")
    assert task["confirmed_decisions_policy"] == "将已确认的具体取舍作为固定前提，仅设计尚未确认的实现细节。"
    assert context["other_topics"] == [{"id": "risk", "title": "风险指标"}]
    assert elsewhere["goal"] not in service.prompts[0]
    assert context["confirmed_context"] == decisions
    system = service.model_calls[0]["system_prompt"]
    assert "禁止从整阶段首题、创作总览或全文任务重新开始" in system
    assert "不得将它们改写成新的root_goals或再次提问" in system
    assert "不能解除原始输入中的检索限制" in system


@pytest.mark.asyncio
async def test_bound_condition_prefix_cannot_mask_body_copied_from_prepared_question():
    value = candidate(2)
    value["question"]["prompt_body"] = "已准备的全局总览问题？"
    coordinator, service = setup([value, value])
    previous = [value["question"]["prompt_body"]]
    with pytest.raises(BrainstormGenerationError, match="当前节点"):
        await prepare(coordinator, node=node(2), plan={"prepared_question_prompts": previous})
    assert len(service.model_calls) == 2
    assert all("question" in call["json_schema"]["properties"] for call in service.model_calls)


@pytest.mark.asyncio
async def test_copy_only_rewrites_body_with_exact_remaining_budget_and_immutable_parent_prefix():
    requested = node(3, 3)
    requested["ancestor_conditions"][0]["option_label"] = "保留原始指标口径与数据来源的趋势图表达"
    requested["ancestor_conditions"][1]["option_label"] = "采用实心圆标记缺失值并完整显示图例名称"
    value = candidate(3, 3, choice="reference:m1+m2")
    value["question"]["prompt_body"] = "继续比较原有图例布局与缺失标记的表示方法如何安排？" * 4
    prefix = BrainstormStageCoordinator._node_condition_prefix(requested)
    coordinator, service = setup([value, {"replacements": {"q0_prompt": "如何排图例？"}}], MEMORY)
    result = await prepare(coordinator, node=requested, limits={"max_depth": 3})
    assert result["question"]["prompt"] == prefix + "如何排图例？"
    assert len(result["question"]["prompt"]) <= 60
    context = json.loads(service.prompts[1])
    assert context["fields"]["q0_prompt"]["text"] == value["question"]["prompt_body"]
    assert context["fields"]["q0_prompt"]["bound_condition_prefix"] == prefix
    assert context["fields"]["q0_prompt"]["max_chars"] == 60 - len(prefix)
    assert service.model_calls[1]["json_schema"]["properties"]["replacements"]["properties"]["q0_prompt"]["maxLength"] == 60 - len(prefix)
    assert MEMORY["sources"][1]["content"] in result["question"]["options"][0]["details"]
    assert result["question"]["parent_question_id"] == requested["parent_question_id"]


@pytest.mark.asyncio
async def test_copy_cannot_rewrite_or_double_fixed_parent_prefix():
    requested = node(2)
    value = candidate(2)
    value["question"]["prompt_body"] = "当前父条件下必须保留完整的问题和具体取舍如何进一步配置？" * 3
    prefix = BrainstormStageCoordinator._node_condition_prefix(requested)
    coordinator, service = setup([value,
        {"replacements": {"q0_prompt": prefix + "如何安排节奏？"}},
        {"replacements": {"q0_prompt": "如何安排节奏？"}}])
    result = await prepare(coordinator, node=requested)
    assert result["question"]["prompt"] == prefix + "如何安排节奏？"
    assert len(service.model_calls) == 3


def test_node_model_schema_exposes_only_question_body_and_binds_complete_conditions_in_code():
    requested = node(3, 3)
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        {**BrainstormStageCoordinator.DEFAULT_LIMITS, "max_depth": 3})
    fields = schema["properties"]["question"]["properties"]
    assert "prompt_body" in fields and "prompt" not in fields
    assert "ancestor_conditions" not in fields
    value = candidate(3, 3)
    candidate_tree, _ = BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "detail",
        {**BrainstormStageCoordinator.DEFAULT_LIMITS, "max_depth": 3})
    prompt = candidate_tree["questions"][0]["prompt"]
    assert prompt == "若「从人物入手」且「采用内心视角」，如何安排节奏？"
    assert value["question"]["prompt_body"] == "如何安排节奏？"


@pytest.mark.parametrize("body_source", ["prior_bodies", "parent"])
@pytest.mark.asyncio
async def test_raw_body_is_checked_against_trusted_prior_bodies_and_parent_prompt(body_source):
    requested = node(2)
    body = "如何配置缺失点标记？"
    prior = {"prepared_question_prompts": ["若「其他方案」，" + body],
        "prepared_question_bodies": [body]}
    if body_source == "parent":
        prior = {}
        requested["ancestor_conditions"][0]["parent_prompt"] = body
    value = candidate(2)
    value["question"]["prompt_body"] = body
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, node=requested, plan=prior)
    assert len(service.model_calls) == 2
    assert all("question" in call["json_schema"]["properties"] for call in service.model_calls)


@pytest.mark.asyncio
async def test_no_guessing_body_from_user_written_quoted_conditional_root():
    prior = "若「库存为空」，如何配置缺失点标记？"
    value = candidate()
    value["question"]["prompt_body"] = "如何配置缺失点标记？"
    coordinator, service = setup([value])
    result = await prepare(coordinator, plan={"prepared_question_prompts": [prior],
        "prepared_question_bodies": [prior]})
    assert result["question"]["prompt"] == value["question"]["prompt_body"]
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_quoted_parent_label_is_bound_exactly_without_regex_or_punctuation_normalization():
    requested = node(2)
    requested["ancestor_conditions"][0]["option_label"] = "方案「A」，保留GPU"
    coordinator, service = setup([candidate(2)])
    result = await prepare(coordinator, node=requested)
    assert result["question"]["prompt"] == "若「方案「A」，保留GPU」，采用什么视角？"
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_trusted_ancestor_strings_keep_leading_and_trailing_spaces_exactly():
    requested = node(2)
    requested["ancestor_conditions"][0].update(option_label=" 方案「A」，GPU ",
        option_description=" 保留真实说明及空白 ", parent_prompt=" 真实父题？ ")
    coordinator, service = setup([candidate(2)])
    result = await prepare(coordinator, node=requested)
    assert result["question"]["prompt"] == "若「 方案「A」，GPU 」，采用什么视角？"
    context = json.loads(service.prompts[0])
    assert context["generation_task"]["ancestor_conditions"] == requested["ancestor_conditions"]
    assert "condition_prefix" not in context["generation_task"]
    assert context["generation_task"]["prompt_body_max_chars"] == 60 - len("若「 方案「A」，GPU 」，")
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_short_effective_body_fits_long_two_level_condition_without_truncation():
    requested = node(3, 3)
    for index, condition in enumerate(requested["ancestor_conditions"]):
        condition["option_label"] = "原" * 23 + str(index)
    value = candidate(3, 3)
    value["question"]["prompt_body"] = "怎样？"
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested, limits={"max_depth": 3})
    assert result["question"]["prompt"].endswith("怎样？")
    assert len(result["question"]["prompt"]) == 58
    assert len(service.model_calls) == 1


def test_conditions_that_fill_entire_body_budget_are_rejected_without_truncation():
    raw = {"ancestor_conditions": [{"option_label": "原" * 29}, {"option_label": "另" * 29}]}
    with pytest.raises(BrainstormGenerationError, match="不能删除条件"):
        BrainstormStageCoordinator._node_condition_prefix(raw)


@pytest.mark.asyncio
async def test_short_raw_body_cannot_repeat_exact_fixed_prefix_before_binding():
    requested = node(2)
    prefix = BrainstormStageCoordinator._node_condition_prefix(requested)
    value = candidate(2)
    value["question"]["prompt_body"] = prefix + "采用什么视角？"
    assert len(prefix + value["question"]["prompt_body"]) < 60
    coordinator, service = setup([value, value])
    with pytest.raises(BrainstormGenerationError, match="当前节点"):
        await prepare(coordinator, node=requested)
    assert len(service.model_calls) == 2
    assert all("question" in call["json_schema"]["properties"] for call in service.model_calls)


@pytest.mark.parametrize("body", ["若「其他方案」，采用什么视角？", "若从人物入手，采用什么视角？"])
@pytest.mark.asyncio
async def test_only_literal_same_prefix_is_rejected_not_similar_user_written_body(body):
    requested = node(2)
    prefix = BrainstormStageCoordinator._node_condition_prefix(requested)
    value = candidate(2)
    value["question"]["prompt_body"] = body
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested)
    assert result["question"]["prompt"] == prefix + body
    assert len(service.model_calls) == 1


def test_grouped_model_schema_couples_each_option_to_its_own_nonempty_goal_array():
    requested = {**node(), "max_options": 5, "max_next_goals": 5}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert set(schema["properties"]) == {"question"}
    assert schema["required"] == ["question"]
    options = schema["properties"]["question"]["properties"]["options"]
    assert options["minItems"] == 2 and options["maxItems"] == 5
    goals = options["items"]["properties"]["next_goals"]
    assert goals["minItems"] == 1 and goals["maxItems"] == 4
    assert goals["items"] == {"type": "string"}
    assert "option_index" not in options["items"]["properties"]
    assert decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(3, 2)), schema)
    assert decoder_array_budget_accepts(provider_candidate(minimal_node_candidate(5, 1)), schema)
    missing = provider_candidate(minimal_node_candidate(3, 1))
    missing["question"]["options"][2]["next_goals"] = []
    assert not decoder_array_budget_accepts(missing, schema)


@pytest.mark.parametrize("index", [0, 6, True, "1"])
def test_public_flat_binder_keeps_rejecting_indices_and_new_provider_decoder_rejects_flat_shape(index):
    requested = {**node(), "max_options": 5, "max_next_goals": 5}
    schema = BrainstormStageCoordinator._node_schema(requested, NO_MEMORY,
        BrainstormStageCoordinator.DEFAULT_LIMITS)
    value = minimal_node_candidate(2, 1)
    value["next_goals"][0]["option_index"] = index
    assert not decoder_array_budget_accepts(value, schema)
    with pytest.raises(BrainstormGenerationError, match="真实选项顺序"):
        BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "detail",
            BrainstormStageCoordinator.DEFAULT_LIMITS)


@pytest.mark.parametrize("mutation", [
    lambda value: value["next_goals"][0].update(option_index=5),
    lambda value: value["next_goals"][1].update(option_index=1),
    lambda value: value["next_goals"][0].update(option_id="a"),
    lambda value: value["question"]["options"][0].update(next_goals=["混用旧协议"]),
])
def test_flat_runtime_rejects_unused_option_indices_missing_coverage_and_mixed_protocol(mutation):
    requested = {**node(), "max_options": 5, "max_next_goals": 5}
    value = minimal_node_candidate(2, 1)
    mutation(value)
    before = copy.deepcopy(value)
    with pytest.raises(BrainstormGenerationError):
        BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "detail",
            BrainstormStageCoordinator.DEFAULT_LIMITS)
    assert value == before


@pytest.mark.asyncio
async def test_three_options_five_aggregate_goals_preserve_ordinal_groups_and_recommendation_sort():
    requested = {**node(), "max_options": 5, "max_next_goals": 5}
    value = minimal_node_candidate(3, 1)
    set_goals(value, 1, ["比较趋势线样式", "安排趋势标记"])
    set_goals(value, 3, ["确定图例位置", "确定图例排版"])
    value["question"]["options"][0]["recommended"] = False
    value["question"]["options"][2]["recommended"] = True
    before = copy.deepcopy(value)
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested)
    assert [option["id"] for option in result["question"]["options"]] == ["c", "a", "b"]
    assert result["next_goals"] == [{"option_id": "a", "goals": ["比较趋势线样式", "安排趋势标记"]},
        {"option_id": "b", "goals": ["具体决定1-0"]}, {"option_id": "c", "goals": ["确定图例位置", "确定图例排版"]}]
    assert len(service.model_calls) == 1
    assert value == before
    assert "每个option自己的next_goals只下钻该option" in json.loads(service.prompts[0])["generation_task"]["task"]


def check_result(verdict="pass", violation="none", decision_ids=None, reason="", *, unit_ids=None, target=None, option_labels=None):
    unit_ids = list(unit_ids or ["q0_prompt", "q0_space", "q0_o0", "q0_o1", "g0_0", "g0_1", "g1_0"])
    checks = {identifier: "pass" for identifier in unit_ids}
    if verdict == "reject":
        checks[target or unit_ids[0]] = violation + (":" + decision_ids[0] if decision_ids else "")
    elif violation != "none" or decision_ids:
        return {"checks": checks, "invalid_global_verdict": verdict}
    certificates = {}
    for identifier in unit_ids:
        if identifier.endswith("_space"):
            certificates[identifier] = {"relation": "joint_fixed_with_new_detail", "decision_ids": [],
                "options": {"a": {"fixed_id": "none", "option_evidence": (option_labels or {}).get("a", "从人物入手"), "effect": "unrelated"},
                    "b": {"fixed_id": "none", "option_evidence": (option_labels or {}).get("b", "从场景入手"), "effect": "unrelated"}}}
    return {"checks": checks, **({"decision_spaces": certificates} if certificates else {})}


def review_units(count=1):
    return [key for qi in range(count) for key in ("q{}_prompt".format(qi), "q{}_space".format(qi), "q{}_o0".format(qi), "q{}_o1".format(qi))]


def fixed_decision(question="已确定采用什么设计规则？", answer="保留已经确认的规则"):
    return {"question_id": "fixed", "question": question, "answer": answer, "answer_source": "user",
        "selected_options": [{"id": "chosen", "label": answer, "description": "完整原说明与必要取舍", "details": "RAG长正文不得复制到checker"}]}


@pytest.mark.parametrize("domain,question,answer,wrong_label", [
    ("棋盘表现", "棋盘是否固定十乘十？", "固定十乘十", "改为八乘八棋盘"),
    ("财务呈现", "核算已定年度预算的方法？", "预算固定为十万元", "将总预算提高至二十万元"),
    ("活动组织", "是否固定面向内部员工？", "只面向内部员工", "改为面向公众推广"),
])
@pytest.mark.asyncio
async def test_independent_checker_rejects_conflicting_nonrecommended_choices_in_any_domain(domain, question, answer, wrong_label):
    bad = candidate()
    bad["question"]["options"][1]["label"] = wrong_label
    good = candidate()
    rejected = check_result("reject", "confirmed_decision_conflict", ["decision_1"], "私密失败自由文本不能进入日志或修复指令")
    rejected["decision_spaces"]["q0_space"]["options"]["b"]["option_evidence"] = wrong_label
    coordinator, service = setup([bad, rejected, good, check_result()], check_consistency=True)
    decision = fixed_decision(question, answer)
    before = copy.deepcopy(decision)
    result = await prepare(coordinator, topic={"id": "topic", "title": domain, "goal": "细化既定设计的实现方式"}, decisions=[decision])
    assert result["question"]["options"][1]["label"] == good["question"]["options"][1]["label"]
    assert len(service.model_calls) == 4
    checked = json.loads(service.prompts[1])
    assert checked["question_contexts"]["q0"]["question"]["options"][1]["label"] == wrong_label
    assert checked["question_contexts"]["q0"]["question"]["options"][1]["recommended"] is False
    assert checked["authority"]["fixed_decisions"][0]["selected_options"][0]["description"] == "完整原说明与必要取舍"
    assert "RAG长正文不得复制到checker" not in service.prompts[1]
    repair = json.loads(service.prompts[2])["validation_failures"][0]
    assert repair["code"] == "confirmed_decision_conflict" and repair["decision_ids"] == ["decision_1"]
    assert "私密失败自由文本" not in service.prompts[2]
    assert decision == before
    assert [call["num_predict"] for call in service.model_calls] == [2400, 1200, 2400, 1200]


@pytest.mark.asyncio
async def test_topic_plan_checker_checks_every_root_and_keeps_confirmed_scope():
    bad = {"root_axis_ids": ["content_focus", "audience"]}
    configured = {axis["id"]: axis for axis in load_root_axes_binding()["config"]["axes"]}
    manifest = axis_plan_manifest("direction", bad["root_axis_ids"], {"max_topics": 1})
    coordinator, service = setup([check_result("reject", "confirmed_decision_reopened", ["decision_1"],
        unit_ids=["root0", "root1"])], check_consistency=True)
    decision = fixed_decision(question="已确定面向哪些阅读对象？", answer="仅面向内部员工")
    from creation.brainstorm_stages import _StageConsistencyError
    with pytest.raises(_StageConsistencyError) as error:
        await prepare(coordinator, stage="direction", kind="topic_plan", root_limit=2, decisions=[decision],
            topic=manifest["topics"][0], plan={"topics": manifest["topics"]}, limits={"max_topics": 1})
    assert error.value.code == "confirmed_decision_reopened" and error.value.decision_ids == ["decision_1"]
    checked = json.loads(service.prompts[0])
    assert [unit["text"] for unit in checked["validation_task"]["units"]] == [
        configured[identifier]["goal"] for identifier in bad["root_axis_ids"]]
    assert checked["authority"]["fixed_decisions"][0]["answer"] == decision["answer"]
    assert len(service.model_calls) == 1 and service.model_calls[0]["num_predict"] == 1200


@pytest.mark.asyncio
async def test_checker_sees_final_copy_and_every_future_goal_without_recalled_bodies():
    value = candidate(choice="reference:m1+m2")
    value["question"]["options"][0]["label"] = "原始过长候选标签必须缩短才能完成当前题目的实际文案显示约束"
    set_goals(value, 2, ["细化已定决定的实现方式"])
    coordinator, service = setup([value, {"replacements": {"q0_o0_label": "完整短方案"}},
        check_result(option_labels={"a": "完整短方案"})], MEMORY, check_consistency=True)
    result = await prepare(coordinator, decisions=[fixed_decision()])
    checked = json.loads(service.prompts[2])
    assert checked["question_contexts"]["q0"]["question"]["options"][0]["label"] == "完整短方案"
    assert checked["validation_task"]["units"][-1]["text"] == "细化已定决定的实现方式"
    assert "details" not in checked["question_contexts"]["q0"]["question"]["options"][0]
    assert MEMORY["sources"][0]["content"] not in service.prompts[2]
    assert MEMORY["sources"][0]["content"] in result["question"]["options"][0]["details"]
    assert len(service.model_calls) == 3


@pytest.mark.parametrize("invalid", [
    {"verdict": "pass"}, {"checks": {}}, {"checks": []},
    {"checks": {"q0_prompt": "pass"}},
    {"checks": {**check_result()["checks"], "forged_unit": "pass"}},
    {"checks": {**check_result()["checks"], "q0_o1": "confirmed_decision_conflict:forged"}},
    {"checks": {**check_result()["checks"], "q0_o1": "invented_code:decision_1"}},
    {"checks": {**check_result()["checks"], "q0_o1": "confirmed_decision_conflict"}},
    {"checks": {**check_result()["checks"], "q0_o1": {"verdict": "pass"}}},
    {"checks": {**check_result()["checks"], "q0_o1": []}},
    {**check_result(), "reason": "私密自由文字"},
    {**check_result(), "verdict": "pass"},
    {"checks": {**check_result()["checks"], "q0_o1": "长" * 121}},
])
@pytest.mark.asyncio
async def test_invalid_checker_output_never_publishes_and_has_no_checker_retry(invalid, caplog):
    caplog.set_level("INFO", logger="creation.brainstorm_stages")
    coordinator, service = setup([candidate(), invalid, candidate(), invalid], check_consistency=True)
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, decisions=[fixed_decision(answer="私密确认文字")])
    assert len(service.model_calls) == 4
    assert sum(call["num_predict"] == 1200 for call in service.model_calls) == 2
    assert "checker_invalid" in caplog.text and "私密确认文字" not in caplog.text


def test_fixed_authority_preserves_manual_edits_exclusions_clears_and_all_user_meanings():
    original = [fixed_decision(answer="原答案"),
        {"question_id": "excluded", "question": "是否对外宣传本次实验？", "answer": "不要对外宣传，只保留内部验证", "user_inputs": ["不扩展到公众渠道"], "answer_source": "user_excluded", "dimension": "活动范围"},
        {"question_id": "cleared", "question": "是否仍限制使用原先体裁？", "answer": "用户清空原体裁约束", "user_inputs": ["不恢复旧决定"], "answer_source": "user_cleared"},
        {"question_id": "assumption", "question": "推断问题", "answer": "未经用户确认的假设", "answer_source": "agent_assumption"},
        {"question_id": "edited-assumption", "question": "原假设范围？", "answer": "旧猜测", "answer_source": "agent_assumption"}]
    before = copy.deepcopy(original)
    fixed, cleared = BrainstormStageCoordinator._stage_authority(original,
        {"fixed": "人工修订的完整新答案", "edited-assumption": "用户明确修订的取舍", "root_request": "", "open_flags": ""})
    assert fixed[0]["answer"] == "人工修订的完整新答案"
    assert fixed[0]["selected_options"][0]["description"] == "完整原说明与必要取舍"
    assert "以当前人工answer为准" in fixed[0]["selected_options_status"]
    assert fixed[1]["question"] == original[1]["question"] and fixed[1]["answer"] == original[1]["answer"]
    assert fixed[1]["user_inputs"] == original[1]["user_inputs"]
    assert cleared[0]["question"] == original[2]["question"] and cleared[0]["answer"] == original[2]["answer"]
    assert {item["question_id"] for item in fixed} == {"fixed", "excluded", "edited-assumption"}
    assert {item["question_id"] for item in cleared} == {"cleared", "root_request", "open_flags"}
    assert original == before


@pytest.mark.asyncio
async def test_all_fourteen_fixed_decisions_are_complete_in_final_generation_task_and_checker():
    decisions = [fixed_decision(question="完整问题{}".format(index), answer="完整答案{}".format(index)) for index in range(14)]
    for index, decision in enumerate(decisions):
        decision["question_id"] = "q{}".format(index)
    coordinator, service = setup([candidate(), check_result()], check_consistency=True)
    await prepare(coordinator, decisions=decisions)
    context = json.loads(service.prompts[0])
    generated = context["generation_task"]["fixed_decisions"]
    checked = json.loads(service.prompts[1])["authority"]["fixed_decisions"]
    assert len(generated) == len(checked) == 14
    assert [item["question"] for item in context["confirmed_context"]] == [item["question"] for item in decisions]
    assert [item["answer"] for item in context["confirmed_context"]] == [item["answer"] for item in decisions]
    assert [item["confirmed_context_index"] for item in generated] == list(range(14))
    assert [item["decision_id"] for item in generated] == [item["decision_id"] for item in checked]
    assert all("question" not in item and "answer" not in item for item in generated)
    choices = service.model_calls[1]["json_schema"]["properties"]["checks"]["properties"]["q0_prompt"]["enum"]
    assert all("confirmed_decision_conflict:decision_{}".format(index) in choices for index in range(1, 15))
    assert "每个局部图表必须混合全部来源" in service.model_calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_checker_timeout_uses_shared_deadline_and_cancels_without_extra_attempt(monkeypatch):
    closed = asyncio.Event()
    class SlowCheck(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            if len(self.model_calls) == 1:
                yield json.dumps(provider_candidate(candidate()), ensure_ascii=False)
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = SlowCheck([])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(NO_MEMORY)
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.04)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator)
    assert closed.is_set() and len(service.model_calls) == 2
    assert service.model_calls[-1]["num_predict"] == 1200


@pytest.mark.asyncio
async def test_generation_omits_literal_copyable_condition_template_but_returns_bound_prompt():
    requested = node(2)
    coordinator, service = setup([candidate(2), check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, node=requested)
    task = json.loads(service.prompts[0])["generation_task"]
    prefix = BrainstormStageCoordinator._node_condition_prefix(requested)
    assert "condition_prefix" not in task and prefix not in service.prompts[0]
    assert task["ancestor_conditions"] == requested["ancestor_conditions"]
    assert task["prompt_body_max_chars"] == 60 - len(prefix)
    assert result["question"]["prompt"].startswith(prefix)
    assert json.loads(service.prompts[1])["question_contexts"]["q0"]["question"]["prompt"] == result["question"]["prompt"]


@pytest.mark.asyncio
async def test_checker_rejects_future_goal_that_reopens_a_confirmed_choice():
    bad = candidate()
    set_goals(bad, 2, ["重新决定既定受众"])
    coordinator, service = setup([bad, check_result("reject", "confirmed_decision_reopened", ["decision_1"]),
        candidate(), check_result()], check_consistency=True)
    result = await prepare(coordinator, decisions=[fixed_decision(answer="受众固定为内部员工")])
    assert json.loads(service.prompts[1])["validation_task"]["units"][-1]["text"] == "重新决定既定受众"
    assert result["next_goals"][1]["goals"] == ["比较场景节奏"]
    assert len(service.model_calls) == 4


@pytest.mark.asyncio
async def test_checker_rejects_option_contradicting_true_parent_condition():
    bad = candidate(2)
    bad["question"]["options"][1]["label"] = "完全排除人物表达"
    coordinator, service = setup([bad, check_result("reject", "parent_condition_conflict", unit_ids=review_units(), option_labels={"b": "完全排除人物表达"}),
        candidate(2), check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, node=node(2))
    checker = json.loads(service.prompts[1])
    assert checker["question_contexts"]["q0"]["ancestor_conditions"] == node(2)["ancestor_conditions"]
    assert checker["authority"]["fixed_decisions"] == []
    assert result["question"]["options"][1]["label"] == "从场景入手"


@pytest.mark.asyncio
async def test_checker_transport_failure_is_safe_closed_and_provider_text_is_not_logged(caplog):
    caplog.set_level("INFO", logger="creation.brainstorm_stages")
    class BrokenCheck(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            self.prompts.append(kwargs["user_prompt"])
            if kwargs["num_predict"] == 1200:
                raise RuntimeError("provider-private-body-and-secret")
            yield json.dumps(provider_candidate(candidate()), ensure_ascii=False)
    service = BrokenCheck([])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(NO_MEMORY)
    async def legacy_relationship(**kwargs):
        return None
    coordinator._check_new_decision_space = legacy_relationship
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator)
    assert len(service.model_calls) == 4
    assert "checker_unavailable" in caplog.text
    assert "provider-private-body-and-secret" not in caplog.text
    assert all("provider-private-body-and-secret" not in prompt for prompt in service.prompts)


@pytest.mark.asyncio
async def test_bad_json_checker_response_never_returns_a_candidate():
    coordinator, service = setup([candidate(), check_result(), candidate(), check_result()], check_consistency=True)
    service.responses[1] = "{"
    service.responses[3] = "{"
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator)
    assert len(service.model_calls) == 4


def test_financial_exclusion_keeps_narrow_original_question_and_answer_scope():
    excluded = {"question_id": "exclude", "answer_source": "user_excluded", "dimension": "预算",
        "question": "是否单独规划境外市场的营销预算？", "answer": "只排除境外营销预算，不排除境内研发预算",
        "user_inputs": ["国内研发预算仍保留"]}
    fixed, cleared = BrainstormStageCoordinator._stage_authority([excluded], {})
    assert fixed[0]["question"] == excluded["question"]
    assert fixed[0]["answer"] == excluded["answer"]
    assert fixed[0]["user_inputs"] == excluded["user_inputs"]
    assert "确认事实" in fixed[0]["constraint"]
    assert cleared == []


def review_record(selected_topic=None, depth=1):
    selected_topic = topic() if selected_topic is None else selected_topic
    requested = node(depth)
    value, _ = BrainstormStageCoordinator._bind_node_candidate(candidate(depth), requested, selected_topic,
        "detail", BrainstormStageCoordinator.DEFAULT_LIMITS)
    question = value["questions"][0]
    question["context_details"] = "不应向checker复制的长来源总述"
    for option in question["options"]:
        option["details"] = "不应向checker复制的长来源原文"
    return {"question": question, "topic": selected_topic, "ancestor_conditions": requested["ancestor_conditions"]}


@pytest.mark.parametrize("title", ["财务核算结构", "童话表现结构", "一般活动安排"])
@pytest.mark.asyncio
async def test_readonly_old_pool_review_pass_keeps_valid_paid_material_exactly_and_never_retrieves(title):
    selected = {**topic(), "title": title}
    records = [review_record(selected), review_record(selected, 2)]
    before = copy.deepcopy(records)
    coordinator, service = setup([check_result(unit_ids=review_units(2))], check_consistency=True)
    coordinator._retrieve_memory = lambda *args: pytest.fail("read-only review must not retrieve")
    result = await prepare(coordinator, kind="review", topic=None, node=None,
        plan={"topics": [selected]}, review_questions=records, decisions=[fixed_decision()])
    assert result == {"review": "pass", "codes": [], "decision_ids": []}
    assert records == before and len(service.model_calls) == 1
    assert service.model_calls[0]["num_predict"] == 1200
    context = json.loads(service.prompts[0])
    assert len(context["question_contexts"]) == 2
    assert context["question_contexts"]["q1"]["ancestor_conditions"] == records[1]["ancestor_conditions"]
    assert "不应向checker复制的长来源" not in service.prompts[0]
    assert "重新验证既存引用" in service.model_calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_readonly_old_pool_semantic_reject_returns_only_codes_for_core_archive_without_rewriting():
    records = [review_record()]
    before = copy.deepcopy(records)
    coordinator, service = setup([check_result("reject", "confirmed_decision_reopened", ["decision_1"], unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, kind="review", topic=None, plan={"topics": [topic()]},
        review_questions=records, decisions=[fixed_decision()])
    assert result == {"review": "reject", "codes": ["confirmed_decision_reopened"], "decision_ids": ["decision_1"]}
    assert records == before and len(service.model_calls) == 1
    assert "reason" not in result


@pytest.mark.parametrize("verdict", [check_result("reject", "insufficient_context", unit_ids=review_units()),
    {"verdict": "pass"}, check_result("pass", "confirmed_decision_conflict", ["decision_1"], unit_ids=review_units())])
@pytest.mark.asyncio
async def test_old_pool_invalid_or_unknown_review_fails_closed_instead_of_archiving(verdict):
    records = [review_record()]
    before = copy.deepcopy(records)
    coordinator, service = setup([verdict], check_consistency=True)
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, kind="review", plan={"topics": [topic()]},
            review_questions=records, decisions=[fixed_decision()])
    assert len(service.model_calls) == 1 and records == before


@pytest.mark.parametrize("count", [0, 13])
@pytest.mark.asyncio
async def test_old_pool_review_batch_capacity_has_no_generation_or_retrieval(count):
    coordinator, service = setup([], check_consistency=True)
    coordinator._retrieve_memory = lambda *args: pytest.fail("review must not retrieve")
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, kind="review", plan={"topics": [topic()]},
            review_questions=[review_record() for _ in range(count)])
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_authority_ids_remain_raw_request_indices_when_forbidden_memory_filters_assumptions():
    decisions = [{"question_id": "assumed", "answer_source": "agent_assumption", "answer": "旧推测"},
        fixed_decision(answer="用户有效约束")]
    coordinator, service = setup([candidate(), check_result()], check_consistency=True)
    await prepare(coordinator, root_request="只用当前输入，不检索个人资料", decisions=decisions)
    for prompt in service.prompts:
        context = json.loads(prompt)
        task = context.get("generation_task", context.get("authority"))
        assert [item["decision_id"] for item in task["fixed_decisions"]] == ["decision_2"]
    choices = service.model_calls[1]["json_schema"]["properties"]["checks"]["properties"]["q0_prompt"]["enum"]
    assert "confirmed_decision_conflict:decision_2" in choices
    assert "confirmed_decision_conflict:decision_1" not in choices


@pytest.mark.asyncio
async def test_review_endpoint_passes_existing_material_to_foreground_queue(monkeypatch):
    app = importlib.import_module("creation.app")
    captured = []
    async def stage_batch(**kwargs):
        captured.append(kwargs)
        return {"review": "pass", "codes": [], "decision_ids": []}
    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    records = [review_record()]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json={"root_request": "短篇",
            "stage": "detail", "kind": "review", "plan": {"topics": [topic()]}, "context_id": CONTEXT_ID,
            "review_questions": records})
    assert response.status_code == 200
    assert captured[0]["review_questions"] == records and captured[0]["kind"] == "review"
    await asyncio.to_thread(queue.worker.join, 1)
    assert not queue.worker.is_alive()


@pytest.mark.asyncio
async def test_revoked_permission_review_rejects_without_retrieval_model_or_old_pool_changes():
    records = [review_record()]
    records[0]["question"]["options"][0].update(memory_mode="reference", memory_ids=["m1"],
        details="撤权前的私有来源原文")
    before = copy.deepcopy(records)
    coordinator, service = setup([], check_consistency=True)
    coordinator._retrieve_memory = lambda *args: pytest.fail("revoked review must not retrieve")
    result = await prepare(coordinator, kind="review", root_request="只用当前输入，不检索个人资料",
        review_questions=records, plan={"topics": [topic()]})
    assert result == {"review": "reject", "codes": ["source_permission_revoked"], "decision_ids": []}
    assert records == before and service.model_calls == []


@pytest.mark.asyncio
async def test_model_cannot_forge_program_only_source_permission_revoked_code():
    forged = check_result("reject", "source_permission_revoked", unit_ids=review_units())
    coordinator, service = setup([forged], check_consistency=True)
    with pytest.raises(BrainstormGenerationError):
        await prepare(coordinator, kind="review", review_questions=[review_record()], plan={"topics": [topic()]})
    assert "source_permission_revoked" not in service.model_calls[0]["json_schema"]["properties"]["checks"]["properties"]["q0_prompt"]["enum"]
    assert len(service.model_calls) == 1


@pytest.mark.parametrize("domain", ["棋盘规则", "支出核算", "舞台布局"])
@pytest.mark.asyncio
async def test_effective_user_question_duplicate_rejects_old_pool_before_any_checker(domain):
    record = review_record({**topic(), "title": domain})
    original = copy.deepcopy(record)
    decision = fixed_decision(question=record["question"]["prompt"], answer="已固定该具体取舍")
    coordinator, service = setup([], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result == {"review": "reject", "codes": ["confirmed_decision_reopened"], "decision_ids": ["decision_1"]}
    assert service.model_calls == [] and record == original


@pytest.mark.parametrize("source,edits", [("user_cleared", {}), ("user_excluded", {}),
    ("agent_assumption", {}), ("user", {"fixed": ""})])
@pytest.mark.asyncio
async def test_cleared_excluded_and_assumed_questions_never_become_confirmed_duplicate_guard(source, edits):
    record = review_record()
    decision = fixed_decision(question=record["question"]["prompt"])
    decision["answer_source"] = source
    coordinator, service = setup([check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision], brief_edits=edits)
    assert result["review"] == "pass" and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_confirmed_node_body_duplicate_cannot_hide_under_new_trusted_condition_prefix():
    bad = candidate(2)
    decision = fixed_decision(question=bad["question"]["prompt_body"])
    good = candidate(2)
    good["question"]["prompt_body"] = "如何配置内部节奏？"
    coordinator, service = setup([bad, good, check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, node=node(2), decisions=[decision])
    assert result["question"]["prompt"].endswith(good["question"]["prompt_body"])
    assert len(service.model_calls) == 3
    feedback = json.loads(service.prompts[1])["validation_failures"][0]
    assert feedback["code"] == "confirmed_decision_reopened"
    assert feedback["decision_ids"] == ["decision_1"]


@pytest.mark.asyncio
async def test_old_pool_unit_coverage_chunks_keep_all_options_topics_and_ancestors_without_retrieval():
    selected = {**topic(), "goal": "仅细化支出数据的归集方法"}
    records = [review_record(selected, 1 if index % 2 == 0 else 2) for index in range(10)]
    for index, record in enumerate(records):
        record["question"]["prompt"] = "该独立归集决定{}如何实施？".format(index)
    keys = review_units(10)
    responses = [check_result(unit_ids=keys[offset:offset + 8]) for offset in range(0, len(keys), 8)]
    coordinator, service = setup(responses, check_consistency=True)
    coordinator._retrieve_memory = lambda *args: pytest.fail("review must never retrieve")
    original = copy.deepcopy(records)
    result = await prepare(coordinator, kind="review", review_questions=records,
        plan={"topics": [selected]}, decisions=[fixed_decision()])
    assert result["review"] == "pass" and len(service.model_calls) == 5
    covered = []
    for call in service.model_calls:
        context = json.loads(call["user_prompt"])
        assert len(context["validation_task"]["units"]) <= 8 and call["num_predict"] == 1200
        expected = [unit["id"] for unit in context["validation_task"]["units"]]
        schema = call["json_schema"]["properties"]["checks"]
        assert schema["required"] == expected and schema["additionalProperties"] is False
        for unit in context["validation_task"]["units"]:
            material = context["question_contexts"][unit["context_key"]]
            qi = int(unit["context_key"][1:])
            assert material["selected_topic"] == selected
            assert material["ancestor_conditions"] == records[qi]["ancestor_conditions"]
            assert len(material["question"]["options"]) == 2
            if unit["kind"] == "option":
                actual = material["question"]["options"][unit["option_index"]]
                assert actual["id"] == unit["option_id"]
        covered += expected
    assert covered == keys and records == original


@pytest.mark.asyncio
async def test_old_pool_later_nonrecommended_choice_rejection_stops_remaining_unit_checks():
    records = [review_record() for _ in range(4)]
    keys = review_units(4)
    responses = [check_result(unit_ids=keys[:8]), check_result("reject", "confirmed_decision_conflict", ["decision_1"],
        unit_ids=keys[8:16], target="q3_o1")]
    coordinator, service = setup(responses, check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=records,
        plan={"topics": [topic()]}, decisions=[fixed_decision()])
    assert result == {"review": "reject", "codes": ["confirmed_decision_conflict"], "decision_ids": ["decision_1"]}
    assert len(service.model_calls) == 2
    unit = next(unit for unit in json.loads(service.prompts[-1])["validation_task"]["units"] if unit["id"] == "q3_o1")
    question = json.loads(service.prompts[-1])["question_contexts"][unit["context_key"]]["question"]
    assert question["options"][unit["option_index"]]["recommended"] is False


@pytest.mark.parametrize("duplicate_top", [False, True])
@pytest.mark.asyncio
async def test_duplicate_json_checker_keys_cannot_hide_a_prior_reject_with_last_wins_pass(duplicate_top):
    coordinator, service = setup([{}], check_consistency=True)
    checks = check_result(unit_ids=review_units())["checks"]
    encoded = json.dumps(checks, ensure_ascii=False)
    if duplicate_top:
        raw = '{"checks":' + encoded + ',"checks":' + encoded + '}'
    else:
        raw = '{"checks":' + encoded[:-1] + ',"q0_o1":"confirmed_decision_conflict:decision_1","q0_o1":"pass"}}'
    service.responses[0] = raw
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[review_record()],
            plan={"topics": [topic()]}, decisions=[fixed_decision()])
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_later_review_coverage_timeout_uses_one_shared_stage_deadline(monkeypatch):
    closed = asyncio.Event()
    class LaterSlowCheck(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            units = json.loads(kwargs["user_prompt"])["validation_task"]["units"]
            if len(self.model_calls) == 1:
                await asyncio.sleep(0.025)
                yield json.dumps(check_result(unit_ids=[unit["id"] for unit in units]))
            else:
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()
    service = LaterSlowCheck([])
    coordinator = BrainstormStageCoordinator(service)
    async def legacy_relationship(**kwargs):
        return None
    coordinator._check_new_decision_space = legacy_relationship
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.06)
    started = time.monotonic()
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, kind="review", review_questions=[review_record() for _ in range(4)],
            plan={"topics": [topic()]})
    assert time.monotonic() - started < 0.25
    assert closed.is_set() and len(service.model_calls) == 2


def test_configured_large_node_unit_inventory_covers_all_twenty_three_goals_and_recommended_ids():
    value = candidate()
    value["question"]["options"][1]["recommended"] = True
    value["question"]["options"][0]["recommended"] = False
    requested = node()
    requested.update(max_next_goals=23)
    set_goals(value, 1, ["独立后续决定{}".format(index) for index in range(22)])
    set_goals(value, 2, ["另一方法独立后续决定"])
    bound, goals = BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "detail",
        {**BrainstormStageCoordinator.DEFAULT_LIMITS, "max_questions_per_topic": 24})
    question = bound["questions"][0]
    result = {"question": question, "next_goals": goals}
    units, contexts = BrainstormStageCoordinator._consistency_units(result, topic(), {"ancestor_conditions": [], "goal": "当前独立后续决定"})
    future = [unit for unit in units if unit["kind"] == "next_goal"]
    assert len(units) == 27 and len(future) == 23
    assert len([unit for unit in future if unit["branch_option_id"] == "a"]) == 22
    assert len([unit for unit in future if unit["branch_option_id"] == "b"]) == 1
    assert len({unit["id"] for unit in units}) == len(units)
    assert contexts["q0"]["selected_topic"] == topic()
    assert BrainstormStageCoordinator.MAX_CONSISTENCY_UNITS == 8


def selected_rule_decisions(record):
    values = []
    for index, option in enumerate(record["question"]["options"]):
        value = fixed_decision(question="既定规则{}如何确定？".format(index), answer="共同沿用此规则")
        value["question_id"] = "rule{}".format(index)
        value["selected_options"] = [{"label": "使用" + option["label"], "tradeoff": option["description"]}]
        values.append(value)
    return values


@pytest.mark.parametrize("domain", ["棋盘布局", "财务汇总", "展览布置"])
@pytest.mark.asyncio
async def test_complete_fields_contained_in_separate_confirmed_choices_cannot_be_repackaged_as_competitors(domain):
    record = review_record({**topic(), "title": domain})
    decisions = selected_rule_decisions(record)
    original = copy.deepcopy((record, decisions))
    coordinator, service = setup([], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result == {"review": "reject", "codes": ["confirmed_decision_reopened"], "decision_ids": ["decision_1", "decision_2"]}
    assert service.model_calls == [] and (record, decisions) == original


def test_complete_choice_containment_preserves_punctuation_and_does_not_drop_authority_coverage():
    record = review_record()
    decisions = selected_rule_decisions(record)
    decisions = [copy.deepcopy(decisions[index % 2]) for index in range(14)]
    for index, decision in enumerate(decisions):
        decision["question_id"] = "rule{}".format(index)
    fixed, _ = BrainstormStageCoordinator._stage_authority(decisions, {})
    assert BrainstormStageCoordinator._confirmed_option_references({"question": record["question"]}, fixed) == ["decision_1", "decision_2"]
    negative = copy.deepcopy(record["question"])
    negative["options"][0]["label"] = "财务指标"
    negative["options"][0]["description"] = "ROI=-5%"
    fixed[0]["selected_options"] = [{"label": "财务指标", "description": "ROI=5%"}]
    assert BrainstormStageCoordinator._confirmed_option_references({"question": negative}, fixed) == []


@pytest.mark.parametrize("source,edited", [("agent_assumption", False), ("user_excluded", False), ("user", True)])
def test_choice_reuse_guard_never_treats_assumptions_exclusions_or_superseded_manual_options_as_current(source, edited):
    record = review_record()
    fixed, _ = BrainstormStageCoordinator._stage_authority(selected_rule_decisions(record), {})
    for item in fixed:
        item.update(answer_source=source, manually_edited=edited)
    assert BrainstormStageCoordinator._confirmed_option_references({"question": record["question"]}, fixed) == []


@pytest.mark.asyncio
async def test_new_labels_with_old_body_and_new_implementation_pass_relation_certificate_under_shared_rules():
    record = review_record()
    decisions = selected_rule_decisions(record)
    record["question"]["options"][0].update(label="用分层索引定位", description=record["question"]["options"][0]["description"] + "，具体用分层索引定位")
    record["question"]["options"][1].update(label="用连续扫描定位", description=record["question"]["options"][1]["description"] + "，具体用连续扫描定位")
    response = check_result(unit_ids=review_units())
    response["decision_spaces"]["q0_space"]["options"]["a"]["option_evidence"] = record["question"]["options"][0]["label"]
    response["decision_spaces"]["q0_space"]["options"]["b"]["option_evidence"] = record["question"]["options"][1]["label"]
    coordinator, service = setup([response], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result["review"] == "pass" and len(service.model_calls) == 1
    schema = service.model_calls[0]["json_schema"]
    assert list(schema["properties"]) == ["decision_spaces", "checks"]
    certificate_schema = schema["properties"]["decision_spaces"]["properties"]["q0_space"]
    assert list(certificate_schema["properties"]) == ["options", "relation", "decision_ids"]
    assert certificate_schema["properties"]["options"]["required"] == ["a", "b"]


@pytest.mark.parametrize("domain", ["棋盘规则", "费用规则", "会议安排"])
@pytest.mark.asyncio
async def test_independent_relation_certificate_rejects_split_joint_rules_even_if_every_basic_unit_says_pass(domain):
    record = review_record({**topic(), "title": domain})
    decisions = selected_rule_decisions(record)
    # A changed wording defeats literal reuse matching, so the independent
    # relation certificate must still reject competing fixed premises.
    for option in record["question"]["options"]:
        option["label"] = "仅采用" + option["label"]
    response = check_result(unit_ids=review_units())
    certificate = response["decision_spaces"]["q0_space"]
    certificate.update(relation="fixed_premises_compete", decision_ids=["decision_1", "decision_2"])
    for index, item in enumerate(certificate["options"].values()):
        item.update(fixed_id="decision_{}".format(index + 1),
            option_evidence=record["question"]["options"][index]["label"], effect="preserves_rule")
    coordinator, service = setup([response], check_consistency=True)
    before = copy.deepcopy(record)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result == {"review": "reject", "codes": ["confirmed_decision_reopened"], "decision_ids": ["decision_1", "decision_2"]}
    assert record == before and len(service.model_calls) == 1


@pytest.mark.parametrize("broken", ["missing_option", "extra_option", "forged_evidence", "unknown_reference", "wrong_role", "unknown_relation"])
@pytest.mark.asyncio
async def test_invalid_relation_certificate_cannot_publish_or_archive_an_unproven_pool(broken):
    response = check_result(unit_ids=review_units())
    certificate = response["decision_spaces"]["q0_space"]
    if broken == "missing_option":
        del certificate["options"]["b"]
    elif broken == "extra_option":
        certificate["options"]["forged"] = copy.deepcopy(certificate["options"]["a"])
    elif broken == "forged_evidence":
        certificate["options"]["b"]["option_evidence"] = "原选项里不存在的新增实现"
    elif broken == "unknown_reference":
        certificate["options"]["b"]["fixed_id"] = "forged"
    elif broken == "wrong_role":
        certificate["options"]["b"]["effect"] = "invented_effect"
    else:
        certificate["relation"] = {}
    coordinator, service = setup([response], check_consistency=True)
    record = review_record()
    original = copy.deepcopy(record)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[fixed_decision()])
    assert record == original and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_generation_authority_is_complete_once_with_current_manual_clear_and_exclusion_field_refs():
    decisions = [fixed_decision(question="完整固定问题", answer="完整固定原答案"),
        {"question_id": "exclude", "question": "精确排除原题范围", "answer": "只排除该范围", "answer_source": "user_excluded"},
        {"question_id": "clear", "question": "精确清空原题范围", "answer": "完整已清空原答案", "answer_source": "user"}]
    edits = {"fixed": "完整当前人工修订答案", "clear": "", "open_flags": ""}
    coordinator, service = setup([candidate(), check_result()], check_consistency=True)
    await prepare(coordinator, decisions=decisions, brief_edits=edits)
    context = json.loads(service.prompts[0])
    task = context["generation_task"]
    assert context["confirmed_context"] == decisions and context["brief_edits"] == edits
    assert task["fixed_decisions"][0]["current_answer_path"] == 'brief_edits["fixed"]'
    assert task["fixed_decisions"][1]["confirmed_context_index"] == 1
    assert all("question" not in item and "answer" not in item and "selected_options" not in item for item in task["fixed_decisions"])
    assert {item["question_id"] for item in task["cleared_constraints"]} == {"clear", "open_flags"}
    assert task["user_constraints"] == {"root_request_path": "root_request", "brief_edits_path": "brief_edits"}
    checked = json.loads(service.prompts[1])["authority"]
    assert checked["fixed_decisions"][0]["answer"] == edits["fixed"]
    assert checked["fixed_decisions"][1]["question"] == decisions[1]["question"]
    assert checked["cleared_constraints"][0]["question"] == decisions[2]["question"]


@pytest.mark.parametrize("framing", ["，用于财务汇总", "，用于画面展示", "，用于会议记录"])
@pytest.mark.asyncio
async def test_existing_labels_with_whole_old_description_plus_framing_remain_confirmed_choices(framing):
    record = review_record()
    decisions = selected_rule_decisions(record)
    for option in record["question"]["options"]:
        option["description"] += framing
    before = copy.deepcopy(record)
    coordinator, service = setup([], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result == {"review": "reject", "codes": ["confirmed_decision_reopened"], "decision_ids": ["decision_1", "decision_2"]}
    assert record == before and service.model_calls == []


@pytest.mark.asyncio
async def test_existing_labels_with_entirely_new_method_descriptions_offer_actual_new_detail():
    record = review_record()
    decisions = selected_rule_decisions(record)
    record["question"]["options"][0]["description"] = "用分层索引定位当前字段并核验索引边界"
    record["question"]["options"][1]["description"] = "用连续扫描定位当前字段并核验扫描范围"
    response = check_result(unit_ids=review_units())
    response["decision_spaces"]["q0_space"]["options"]["a"]["option_evidence"] = record["question"]["options"][0]["label"]
    response["decision_spaces"]["q0_space"]["options"]["b"]["option_evidence"] = record["question"]["options"][1]["label"]
    coordinator, service = setup([response], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result["review"] == "pass" and len(service.model_calls) == 1


def test_full_choice_containment_must_match_label_and_description_from_the_same_authority_pair():
    record = review_record()
    fixed, _ = BrainstormStageCoordinator._stage_authority(selected_rule_decisions(record), {})
    fixed[0]["selected_options"][0]["tradeoff"], fixed[1]["selected_options"][0]["tradeoff"] = (
        fixed[1]["selected_options"][0]["tradeoff"], fixed[0]["selected_options"][0]["tradeoff"])
    assert BrainstormStageCoordinator._confirmed_option_references({"question": record["question"]}, fixed) == []


@pytest.mark.parametrize("domain", ["棋盘实施", "核算实施", "活动实施"])
@pytest.mark.asyncio
async def test_new_methods_preserve_fixed_background_with_program_bound_complete_rule(domain):
    record = review_record({**topic(), "title": domain})
    decisions = [fixed_decision(question="此前固定的目标是什么？", answer="沿用既定目标")]
    response = check_result(unit_ids=review_units())
    for item in response["decision_spaces"]["q0_space"]["options"].values():
        item.update(fixed_id="decision_1", effect="preserves_rule")
    coordinator, service = setup([response, compatibility_facts(), compatibility_facts()], check_consistency=True)
    before = copy.deepcopy(record)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result["review"] == "pass" and len(service.model_calls) == 3 and record == before
    schema = service.model_calls[0]["json_schema"]["properties"]["decision_spaces"]["properties"]["q0_space"]
    assert list(schema["properties"]["options"]["properties"]["a"]["properties"]) == ["option_evidence", "fixed_id", "effect"]
    assert "旧触发条件已经成立" in service.model_calls[0]["system_prompt"]
    assert "是否有真实新决定" in service.model_calls[0]["system_prompt"]
    assert len(json.loads(service.prompts[0])["authority"]["fixed_decisions"]) == 1



def compatibility_facts(kind="conditional_action", **changes):
    return {"fixed_rule_type": kind, "candidate_behavior": "保留既定动作并新增检测记录方法",
        "adds_action_precondition": "no", "changes_required_action": "no",
        "excludes_required_content_or_scope": "no", **changes}


def rule_effect_response(effect="adds_trigger_requirement"):
    record = review_record()
    record["question"]["options"][1].update(label="增加连续发生条件", description="只有连续发生才立即记录并暂停分析")
    decision = fixed_decision(question="出现异常时应如何处理？", answer="立即记录并暂停分析")
    response = check_result(unit_ids=review_units())
    response["decision_spaces"]["q0_space"]["options"]["b"].update(
        fixed_id="decision_1", option_evidence=record["question"]["options"][1]["label"], effect=effect)
    return record, decision, response


@pytest.mark.parametrize("effect", ["adds_trigger_requirement", "changes_action", "narrows_scope", "changes_required_set"])
@pytest.mark.asyncio
async def test_any_bound_changed_option_independently_rejects_even_if_outer_and_all_checks_pass(effect):
    record, decision, response = rule_effect_response(effect)
    original = copy.deepcopy((record, decision))
    coordinator, service = setup([response], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result == {"review": "reject", "codes": ["confirmed_decision_conflict"], "decision_ids": ["decision_1"]}
    assert len(service.model_calls) == 1 and (record, decision) == original
    assert record["question"]["options"][1]["recommended"] is False


@pytest.mark.parametrize("broken", ["unknown_id", "nonstring_id", "forged_evidence", "changed_number", "changed_punctuation", "empty_evidence", "long_evidence", "unknown_effect", "extra_field", "late_invalid", "none_with_changed", "changed_whitespace"])
@pytest.mark.asyncio
async def test_unbound_or_malformed_effect_certificate_fails_closed_without_archiving(broken):
    record, decision, response = rule_effect_response()
    item = response["decision_spaces"]["q0_space"]["options"]["b"]
    if broken == "unknown_id":
        item["fixed_id"] = "forged"
    elif broken == "nonstring_id":
        item["fixed_id"] = []
    elif broken == "forged_evidence":
        item["option_evidence"] = "未经确认的动作"
    elif broken == "changed_number":
        item["option_evidence"] = "连续3次"
    elif broken == "changed_punctuation":
        item["option_evidence"] = "连续、发生"
    elif broken == "empty_evidence":
        item["option_evidence"] = ""
    elif broken == "long_evidence":
        item["option_evidence"] = "长" * 25
    elif broken == "unknown_effect":
        item["effect"] = "invented"
    elif broken == "extra_field":
        item["extra"] = "invented"
    elif broken == "none_with_changed":
        item["fixed_id"] = "none"
    elif broken == "changed_whitespace":
        item["option_evidence"] = "增加 连续发生 条件"
    else:
        first = response["decision_spaces"]["q0_space"]["options"]["a"]
        first.update(fixed_id="decision_1", option_evidence=record["question"]["options"][0]["label"], effect="changes_action")
        item["option_evidence"] = "虚假后续证据"
    coordinator, service = setup([response], check_consistency=True)
    before = copy.deepcopy((record, decision))
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert len(service.model_calls) == 1 and (record, decision) == before


@pytest.mark.asyncio
async def test_current_option_label_is_bound_exactly_including_its_actual_whitespace():
    record, decision, response = rule_effect_response()
    record["question"]["options"][1]["label"] = "增加 连续发生 条件"
    response["decision_spaces"]["q0_space"]["options"]["b"]["option_evidence"] = record["question"]["options"][1]["label"]
    coordinator, service = setup([response], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result["codes"] == ["confirmed_decision_conflict"] and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_effect_certificate_binds_current_manual_full_rule_and_retains_all_authority():
    record, decision, response = rule_effect_response("preserves_rule")
    edits = {"fixed": "继续记录但不暂停分析"}
    coordinator, service = setup([response, compatibility_facts()], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record], brief_edits=edits,
        plan={"topics": [record["topic"]]}, decisions=[decision])
    actual = json.loads(service.prompts[0])["authority"]["fixed_decisions"][0]
    assert actual["answer"] == edits["fixed"] and actual["manually_edited"]
    assert actual["selected_options"] == [{key: value for key, value in option.items() if key != "details"} for option in decision["selected_options"]]
    assert "RAG长正文不得复制到checker" not in service.prompts[0]
    assert result["review"] == "pass" and len(service.model_calls) == 2


@pytest.mark.parametrize("effect", ["unknown", "same_fixed_choice", "fixed_only_rule"])
@pytest.mark.asyncio
async def test_unknown_or_old_only_classification_never_silently_passes(effect):
    record, decision, response = rule_effect_response(effect)
    coordinator, service = setup([response], check_consistency=True)
    expected = "insufficient_context" if effect == "unknown" else "checker_invalid"
    with pytest.raises(BrainstormGenerationError, match=expected):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_preserved_rule_compatibility_never_overrides_reopened_whole_question_relation():
    record, decision, response = rule_effect_response("preserves_rule")
    response["decision_spaces"]["q0_space"].update(relation="fixed_choices_only", decision_ids=["decision_1"])
    coordinator, service = setup([response], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result["codes"] == ["confirmed_decision_reopened"] and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_checker_separates_candidate_justification_for_fact_review_without_mutating_stored_question():
    record = review_record()
    record["question"].update(why_now="候选自称已经遵循前提", single_choice_reason="候选自称必需单选")
    before = copy.deepcopy(record)
    coordinator, service = setup([check_result(unit_ids=review_units())], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record], plan={"topics": [record["topic"]]})
    checked = json.loads(service.prompts[0])["question_contexts"]["q0"]["question"]
    assert "why_now" not in checked and "single_choice_reason" not in checked
    annotations = json.loads(service.prompts[0])["question_contexts"]["q0"]["candidate_copy"]
    assert annotations["why_now"] == record["question"]["why_now"]
    assert annotations["single_choice_reason"] == record["question"]["single_choice_reason"]
    assert result["review"] == "pass" and record == before and len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_focused_compatibility_rejects_real_bound_rule_conflict_after_false_primary_pass():
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(adds_action_precondition="yes")], check_consistency=True)
    before = copy.deepcopy((record, decision))
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result == {"review": "reject", "codes": ["confirmed_decision_conflict"], "decision_ids": ["decision_1"]}
    assert [call["num_predict"] for call in service.model_calls] == [1200, 800]
    focused = json.loads(service.prompts[1])
    assert focused["fixed_decision"]["decision_id"] == "decision_1"
    assert focused["fixed_decision"]["question"] == decision["question"]
    assert focused["fixed_decision"]["answer"] == decision["answer"]
    assert focused["candidate_alternative"] == {"question_prompt": record["question"]["prompt"],
        "option": {key: record["question"]["options"][1][key] for key in ("id", "label", "description")}}
    assert "effect" not in service.prompts[1] and (record, decision) == before


@pytest.mark.asyncio
async def test_focused_compatibility_rechecks_nonnull_rule_even_if_primary_claims_unrelated():
    record, decision, response = rule_effect_response("unrelated")
    coordinator, service = setup([response, compatibility_facts(excludes_required_content_or_scope="yes")], check_consistency=True)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result["codes"] == ["confirmed_decision_conflict"] and len(service.model_calls) == 2


@pytest.mark.parametrize("invalid", [{}, {"fixed_rule_type": "invented"}, {"fixed_rule_type": []}, {"fixed_rule_type": "other", "extra": "invented"}])
@pytest.mark.asyncio
async def test_invalid_focused_compatibility_result_keeps_existing_pool_and_has_no_recheck_retry(invalid):
    record, decision, response = rule_effect_response("preserves_rule")
    before = copy.deepcopy(record)
    coordinator, service = setup([response, invalid], check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert record == before and [call["num_predict"] for call in service.model_calls] == [1200, 800]


@pytest.mark.asyncio
async def test_unknown_focused_compatibility_result_fails_closed_without_archiving():
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(changes_required_action="unknown")], check_consistency=True)
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_primary_keeps_node_scope_and_future_branches_while_focus_isolates_actual_alternative():
    requested = node(2, 3)
    response = check_result()
    response["decision_spaces"]["q0_space"]["options"]["a"].update(fixed_id="decision_1", effect="preserves_rule")
    coordinator, service = setup([candidate(2, 3), response, compatibility_facts()], check_consistency=True)
    result = await prepare(coordinator, node=requested, limits={"max_depth": 3}, decisions=[fixed_decision()])
    focused = json.loads(service.prompts[2])
    primary = json.loads(service.prompts[1])
    assert primary["question_contexts"]["q0"]["node_goal"] == requested["goal"]
    assert primary["question_contexts"]["q0"]["ancestor_conditions"] == requested["ancestor_conditions"]
    future = [unit for unit in primary["validation_task"]["units"] if unit["kind"] == "next_goal"]
    assert [unit["text"] for unit in future] == [goal for group in result["next_goals"] for goal in group["goals"]]
    assert focused["candidate_alternative"]["question_prompt"] == result["question"]["prompt"]
    assert focused["candidate_alternative"]["option"] == {key: result["question"]["options"][0][key] for key in ("id", "label", "description")}
    assert [call["num_predict"] for call in service.model_calls] == [2400, 1200, 800]


@pytest.mark.asyncio
async def test_focused_rule_check_cancellation_uses_existing_whole_step_deadline(monkeypatch):
    record, decision, response = rule_effect_response("preserves_rule")
    class SlowFocusedService(StubCreationService):
        cancelled = False
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            self.prompts.append(kwargs["user_prompt"])
            if kwargs["num_predict"] == 800:
                try:
                    await asyncio.sleep(1)
                finally:
                    self.cancelled = True
            else:
                yield json.dumps(response, ensure_ascii=False)
    service = SlowFocusedService([])
    coordinator = BrainstormStageCoordinator(service)
    async def relationship_pass(**kwargs):
        return None
    coordinator._check_new_decision_space = relationship_pass
    coordinator._retrieve_memory = lambda *args: pytest.fail("review cannot retrieve")
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.04)
    before = copy.deepcopy(record)
    with pytest.raises(BrainstormGenerationTimeout):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert [call["num_predict"] for call in service.model_calls] == [1200, 800]
    assert service.cancelled and record == before


@pytest.mark.asyncio
async def test_focused_legacy_option_preserves_tradeoff_without_fabricating_description():
    record, decision, response = rule_effect_response("preserves_rule")
    value = record["question"]["options"][1].pop("description")
    record["question"]["options"][1]["tradeoff"] = value
    coordinator, service = setup([response, compatibility_facts()], check_consistency=True)
    original = copy.deepcopy(record)
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    focused = json.loads(service.prompts[1])["candidate_alternative"]["option"]
    assert focused["description"] == "" and focused["tradeoff"] == value
    assert record == original and result["review"] == "pass"


@pytest.mark.asyncio
async def test_node_generator_directly_decides_full_current_goal_and_preserves_fixed_trigger_actions():
    requested = {**node(), "goal": "选择尚未确认的实现办法"}
    coordinator, service = setup([candidate()])
    await prepare(coordinator, node=requested)
    prompt = service.model_calls[0]["system_prompt"]
    assert "每个option必须直接回答当前node_goal待决定的做法" in prompt
    assert "不能把当前目标整体推迟到next_goals" in prompt
    assert "新增次数、等待或附加条件不能延迟已经确认的动作" in prompt
    assert "目标本身尚未确认的内容可合法选择" in prompt
    context = json.loads(service.prompts[0])
    assert context["generation_task"]["node_goal"] == requested["goal"]
    assert "goal" not in context["generation_task"] and "candidate_topic" not in context
    assert "selected_topic 仅限定背景和排除边界" in prompt
    assert "不能以其较宽 goal 代替 node_goal" in prompt
    assert "next_goals 只下钻当前真实 option 的条件" in prompt
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_focused_rule_facts_cover_each_bound_option_with_its_own_complete_decision():
    record = review_record()
    decisions = [fixed_decision(question="既定内容要求是什么？", answer="保留全部既定内容"),
        {**fixed_decision(question="检测后必须怎样处理？", answer="发现问题立即记录"), "question_id": "fixed2"}]
    response = check_result(unit_ids=review_units())
    for index, item in enumerate(response["decision_spaces"]["q0_space"]["options"].values()):
        item.update(fixed_id="decision_" + str(index + 1), effect="preserves_rule")
    coordinator, service = setup([response, compatibility_facts(kind="required_content_or_scope"),
        compatibility_facts(kind="other")], check_consistency=True)
    before = copy.deepcopy((record, decisions))
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=decisions)
    assert result["review"] == "pass" and (record, decisions) == before
    assert [call["num_predict"] for call in service.model_calls] == [1200, 800, 800]
    for index in range(2):
        focused = json.loads(service.prompts[index + 1])
        assert focused["fixed_decision"]["decision_id"] == "decision_" + str(index + 1)
        assert focused["fixed_decision"]["answer"] == decisions[index]["answer"]
        assert focused["candidate_alternative"]["option"]["id"] == record["question"]["options"][index]["id"]
        assert "effect" not in service.prompts[index + 1]
        schema = service.model_calls[index + 1]["json_schema"]["properties"]
        assert schema["fixed_rule_type"]["enum"] == ["conditional_action", "required_content_or_scope", "other"]
    assert record["question"]["options"][1]["recommended"] is False


@pytest.mark.parametrize("fact", ["adds_action_precondition", "changes_required_action", "excludes_required_content_or_scope"])
@pytest.mark.asyncio
async def test_unknown_focused_behavior_fact_preserves_pool_instead_of_guessing_compatibility(fact):
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(**{fact: "unknown"})], check_consistency=True)
    before = copy.deepcopy((record, decision))
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert (record, decision) == before and [call["num_predict"] for call in service.model_calls] == [1200, 800]


@pytest.mark.parametrize("kind", ["required_content_or_scope", "other"])
@pytest.mark.asyncio
async def test_nonconditional_rule_cannot_gain_an_invented_trigger_from_checker(kind):
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(kind=kind, adds_action_precondition="yes")], check_consistency=True)
    before = copy.deepcopy(record)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert record == before and len(service.model_calls) == 2


@pytest.mark.parametrize("fact", ["changes_required_action", "excludes_required_content_or_scope"])
@pytest.mark.asyncio
async def test_individual_action_or_scope_violation_rejects_actual_bound_rule_even_for_unrecommended_option(fact):
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(**{fact: "yes"})], check_consistency=True)
    before = copy.deepcopy((record, decision))
    result = await prepare(coordinator, kind="review", review_questions=[record],
        plan={"topics": [record["topic"]]}, decisions=[decision])
    assert result == {"review": "reject", "codes": ["confirmed_decision_conflict"], "decision_ids": ["decision_1"]}
    assert (record, decision) == before and len(service.model_calls) == 2


@pytest.mark.parametrize("change", [
    {"fixed_rule_type": "unknown"}, {"fixed_rule_type": []}, {"candidate_behavior": ""},
    {"candidate_behavior": "文" * 161}, {"adds_action_precondition": []},
    {"changes_required_action": "invented"}, {"extra": "untrusted"},
])
@pytest.mark.asyncio
async def test_malformed_focused_behavior_certificate_fails_without_rewriting_or_retrying(change):
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(**change)], check_consistency=True)
    before = copy.deepcopy(record)
    with pytest.raises(BrainstormGenerationError, match="checker_invalid"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert record == before and len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_unresolved_behavior_fact_keeps_pool_even_when_another_fact_claims_violation():
    record, decision, response = rule_effect_response("preserves_rule")
    coordinator, service = setup([response, compatibility_facts(changes_required_action="yes",
        excludes_required_content_or_scope="unknown")], check_consistency=True)
    before = copy.deepcopy(record)
    with pytest.raises(BrainstormGenerationError, match="insufficient_context"):
        await prepare(coordinator, kind="review", review_questions=[record],
            plan={"topics": [record["topic"]]}, decisions=[decision])
    assert record == before and len(service.model_calls) == 2
