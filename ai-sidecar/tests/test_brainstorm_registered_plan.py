"""Program-owned topics cannot widen one property's scope or add fact work."""
import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_root_axes import RootAxisCompiler, load_root_axes_binding
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator
from tests.brainstorm_axis_fixtures import axis_plan_manifest, axis_plan_raw
from tests.test_creation_brainstorm import StubCreationService

MODULE = importlib.import_module("creation.brainstorm_root_axes")


def limits(**changes):
    return Coordinator.limits(changes)


def compiler():
    return RootAxisCompiler(load_root_axes_binding())


def expected_topic(index, identifiers):
    configured = {axis["id"]: axis for axis in load_root_axes_binding()["config"]["axes"]}
    return {"id": "topic_axis_{}".format(index),
        "title": "与".join(configured[key]["title"] for key in identifiers),
        "goal": "；".join(configured[key]["goal"] for key in identifiers),
        "root_axis_ids": list(identifiers)}


def test_audience_topic_contains_only_its_registered_attribute_and_never_purpose_or_focus():
    c = compiler()
    budget = limits()
    result = c.compile_plan({"axis_ids": ["audience", "purpose", "content_focus"]}, "direction", budget, {})
    assert result == {"topics": [expected_topic(1, ["audience"]), expected_topic(2, ["purpose"]),
        expected_topic(3, ["content_focus"])], "chapters": [], "open_flags": []}
    audience = result["topics"][0]
    roots = c.topic_goals(audience, "direction", budget, 4, result["topics"])
    assert roots == {"root_goals": [expected_topic(1, ["audience"])["goal"]]}
    assert len(roots["root_goals"]) == 1


@pytest.mark.parametrize("raw", [
    {"axis_ids": ["data_consistency"]},
    {"axis_ids": ["audience", "audience"]},
    {"axis_ids": ["information_granularity"]},
    {"axis_ids": ["受众范围"]},
    {"axis_ids": []},
    {"axis_ids": "audience"},
    {"axis_ids": [1]},
    {"axis_ids": [True]},
    {"axis_ids": ["audience " ]},
    {"topics": [{"id": "topic06", "title": "数据口径核验", "goal": "重新决定周期和指标定义"}]},
    {"axis_ids": ["audience"], "topics": []},
    {"axis_ids": ["audience"], "root_goals": ["自由核验目标"]},
    {"axis_ids": ["audience"], "chapters": []},
    {"axis_ids": ["audience"], "open_flags": ["模型声称已确认的对象"]},
])
def test_provider_cannot_add_fact_work_duplicate_attribute_or_free_manifest(raw):
    before = copy.deepcopy(raw)
    with pytest.raises(BrainstormGenerationError):
        compiler().compile_plan(raw, "direction", limits(), {})
    assert raw == before


@pytest.mark.parametrize("count,maximum,expected_sizes", [
    (1, 6, [1]), (5, 6, [1, 1, 1, 1, 1]), (5, 3, [2, 2, 1]),
    (5, 2, [3, 2]), (4, 2, [2, 2]),
])
def test_attribute_order_and_every_selected_attribute_survive_uniform_contiguous_grouping(count, maximum, expected_sizes):
    c = compiler()
    identifiers = [axis["id"] for axis in c.eligible_axes("direction")][:count]
    budget = limits(max_topics=maximum)
    raw = {"axis_ids": identifiers}
    frozen = copy.deepcopy(raw)
    result = c.compile_plan(raw, "direction", budget, {})
    assert [len(topic["root_axis_ids"]) for topic in result["topics"]] == expected_sizes
    assert [key for topic in result["topics"] for key in topic["root_axis_ids"]] == identifiers
    for index, topic in enumerate(result["topics"], 1):
        assert topic == expected_topic(index, topic["root_axis_ids"])
        assert c.topic_goals(topic, "direction", budget, 4, result["topics"])["root_goals"] == [
            expected_topic(1, [key])["goal"] for key in topic["root_axis_ids"]]
    assert result["chapters"] == [] and result["open_flags"] == [] and raw == frozen


def test_whole_stage_duplicate_rejected_even_when_two_occurrences_would_land_in_different_topics():
    raw = {"axis_ids": ["audience", "purpose", "content_focus", "tone", "audience"]}
    with pytest.raises(BrainstormGenerationError):
        compiler().compile_plan(raw, "direction", limits(max_topics=2), {})
    assert raw["axis_ids"][-1] == "audience"


def test_root_budget_never_drops_an_attribute_or_creates_extra_topics():
    raw = {"axis_ids": [axis["id"] for axis in compiler().eligible_axes("direction")]}
    with pytest.raises(BrainstormGenerationError):
        compiler().compile_plan(raw, "direction", limits(max_topics=1, max_questions_per_topic=4), {})
    assert len(raw["axis_ids"]) == 5


@pytest.mark.parametrize("field", ["title", "goal"])
def test_joined_copy_budget_rejects_whole_group_instead_of_truncating(monkeypatch, tmp_path, field):
    # Future implemented capabilities may have valid individual copy lengths;
    # grouping must still enforce the existing public title/goal budget.
    config = load_root_axes_binding()["config"]
    config["axes"] = config["axes"][:4]
    implemented = copy.deepcopy(MODULE.IMPLEMENTED_AXES)
    for index, axis in enumerate(config["axes"]):
        axis[field] = ("未来完整属性" * 4 if field == "title" else "完整已实现目标" * 10) + str(index)
        implemented[axis["id"]][field] = axis[field]
    monkeypatch.setattr(MODULE, "IMPLEMENTED_AXES", implemented)
    path = tmp_path / "future-implemented-registry.json"
    encoded = json.dumps(config, ensure_ascii=False).encode()
    path.write_bytes(encoded)
    monkeypatch.setattr(MODULE, "_registry_path", lambda: path)
    c = RootAxisCompiler({"config_sha256": hashlib.sha256(encoded).hexdigest(), "config": config})
    raw = {"axis_ids": [axis["id"] for axis in config["axes"]]}
    frozen = copy.deepcopy(raw)
    with pytest.raises(BrainstormGenerationError):
        c.compile_plan(raw, "detail", limits(max_topics=1), {})
    assert raw == frozen


@pytest.mark.parametrize("flags,expected", [
    ("", []), ("  ", []), ("第一条完整事项\n\n第二条完整事项", ["第一条完整事项", "第二条完整事项"]),
    ("  用户已确认读者，尚需决定粒度  \n另一真实事项", ["  用户已确认读者，尚需决定粒度  ", "另一真实事项"]),
])
def test_flags_preserve_only_complete_current_user_lines_without_model_rewrite(flags, expected):
    raw, edits = {"axis_ids": ["audience"]}, {"open_flags": flags}
    frozen = copy.deepcopy((raw, edits))
    result = compiler().compile_plan(raw, "direction", limits(), edits)
    assert result["open_flags"] == expected and (raw, edits) == frozen


@pytest.mark.parametrize("flags", [None, [], {}, 42, "x" * 241, "\n".join(str(i) for i in range(13))])
def test_current_flag_format_and_budget_errors_are_not_repaired_into_a_valid_plan(flags):
    with pytest.raises(BrainstormGenerationError):
        compiler().compile_plan({"axis_ids": ["audience"]}, "direction", limits(), {"open_flags": flags})


@pytest.mark.parametrize("change", ["purpose", "focus", "freegoal", "title", "id", "order", "unknown", "duplicate", "extra", "missing_stage_topics"])
def test_bound_audience_topic_rejects_expansion_or_forged_manifest_before_any_model(change):
    c = compiler()
    budget = limits()
    result = c.compile_plan({"axis_ids": ["audience", "purpose"]}, "direction", budget, {})
    topic, topics = copy.deepcopy(result["topics"][0]), copy.deepcopy(result["topics"])
    if change in ("purpose", "focus"):
        topic["root_axis_ids"].append("purpose" if change == "purpose" else "content_focus")
    elif change == "freegoal":
        topic["goal"] = "解决数据周期与指标定义对齐"
    elif change == "title":
        topic["title"] = "模型自由定义主题"
    elif change == "id":
        topic["id"] = "topic06_data_consistency"
    elif change == "order":
        topics.reverse()
    elif change == "unknown":
        topic["root_axis_ids"] = ["audience_alias"]
    elif change == "duplicate":
        topics[1]["root_axis_ids"] = ["audience"]
    elif change == "extra":
        topic["candidate_goal"] = "伪造新范围"
    else:
        topics = []
    with pytest.raises(BrainstormGenerationError):
        c.topic_goals(topic, "direction", budget, 4, topics)


@pytest.mark.parametrize("change", ["goal", "title", "id", "axis_order", "duplicate", "free_topic", "flags", "unknown_field"])
def test_whole_compiled_manifest_is_exact_and_cannot_be_changed_by_a_pass(change):
    c = compiler()
    budget = limits(max_topics=2)
    raw = {"axis_ids": ["audience", "purpose", "content_focus"]}
    edits = {"open_flags": "真实待办原文"}
    result = c.compile_plan(raw, "direction", budget, edits)
    c.verify_plan(result, raw, "direction", budget, edits)
    if change in ("goal", "title", "id"):
        result["topics"][0][change] = "未经程序绑定的改写"
    elif change == "axis_order":
        result["topics"][0]["root_axis_ids"].reverse()
    elif change == "duplicate":
        result["topics"][1]["root_axis_ids"] = result["topics"][0]["root_axis_ids"]
    elif change == "free_topic":
        result["topics"].append({"id": "topic06", "title": "核验数据周期", "goal": "解决统计周期对齐", "root_axis_ids": []})
    elif change == "flags":
        result["open_flags"][0] = "模型重新解释用户原文"
    else:
        result["selected_axes"] = raw["axis_ids"]
    with pytest.raises(BrainstormGenerationError):
        c.verify_plan(result, raw, "direction", budget, edits)


def topic_coordinator(responses=()):
    service = StubCreationService([json.dumps(value, ensure_ascii=False) for value in responses])
    c = Coordinator(service)
    def forbidden(*args, **kwargs):
        pytest.fail("bound roots must not generate free prose or retrieve facts")
    c._retrieve_memory = forbidden
    return c, service


async def bound_roots(c, manifest, **extra):
    return await c.stage_batch(stage="direction", kind="topic_plan", root_request=extra.pop("root_request", "完整原始需求"),
        decisions=extra.pop("decisions", []), topic=manifest["topics"][0], plan={"topics": manifest["topics"]},
        limits=limits(), root_limit=4, context_id="a" * 64,
        root_axes_binding=load_root_axes_binding(), **extra)


@pytest.mark.asyncio
async def test_pure_topic_roots_receive_complete_skill_root_topic_and_fixed_authority_without_generation():
    manifest = axis_plan_manifest("direction", ["audience", "purpose"])
    c, service = topic_coordinator()
    root = "根请求与明确排除必须全部保留" * 200
    skills = [{"id": "policy", "skillDescription": {"rules": ["全部必需字段保留" * 1000]},
        "last_original_policy": "末尾原政策不可丢"}]
    decisions = [{"question_id": "fixed", "question": "既定取舍", "answer": "原完整答案",
        "answer_source": "user", "user_inputs": ["当前用户完整补充"]}]
    seen = []
    async def inspect(**kwargs):
        assert kwargs["task"]["user_constraints"]["root_request"] == root
        assert kwargs["task"]["user_constraints"]["selected_skills"] == skills
        assert kwargs["topic"] == manifest["topics"][0]
        assert kwargs["task"]["fixed_decisions"][0]["answer"] == decisions[0]["answer"]
        assert kwargs["result"] == {"root_goals": [manifest["topics"][0]["goal"]]}
        assert kwargs["task"]["program_bound_root_axes"]
        seen.append(copy.deepcopy(kwargs))
    c._check_stage_consistency = inspect
    frozen = copy.deepcopy((manifest, skills, decisions))
    result = await bound_roots(c, manifest, root_request=root, selected_skills=skills, decisions=decisions)
    assert result == {"root_goals": [manifest["topics"][0]["goal"]]}
    assert len(seen) == 1 and service.model_calls == []
    assert (manifest, skills, decisions) == frozen


@pytest.mark.asyncio
async def test_program_roots_still_reject_a_real_semantic_scope_verdict_without_regeneration():
    from creation.brainstorm_stages import _StageConsistencyError
    manifest = axis_plan_manifest("direction", ["audience"])
    c, service = topic_coordinator([{"checks": {"root0": "topic_scope"}}])
    frozen = copy.deepcopy(manifest)
    with pytest.raises(_StageConsistencyError) as error:
        await bound_roots(c, manifest)
    assert error.value.code == "topic_scope"
    assert len(service.model_calls) == 1 and service.model_calls[0]["temperature"] == 0.0
    payload = json.loads(service.prompts[0])
    assert payload["validation_task"]["units"][0]["text"] == manifest["topics"][0]["goal"]
    assert manifest == frozen


@pytest.mark.asyncio
async def test_checker_cannot_mutate_bound_audience_roots_into_purpose_even_when_it_returns_pass():
    manifest = axis_plan_manifest("direction", ["audience"])
    c, service = topic_coordinator()
    async def mutate(**kwargs):
        kwargs["result"]["root_goals"].append(expected_topic(1, ["purpose"])["goal"])
    c._check_stage_consistency = mutate
    with pytest.raises(BrainstormGenerationError):
        await bound_roots(c, manifest)
    assert service.model_calls == []


@pytest.mark.parametrize("change", ["extra", "empty_chapter", "id", "trimmed_goal", "missing_axis"])
@pytest.mark.asyncio
async def test_actual_current_topic_identity_is_checked_before_display_normalization(change):
    manifest = axis_plan_manifest("direction", ["audience"])
    topic = copy.deepcopy(manifest["topics"][0])
    if change == "extra":
        topic["extra_goal"] = "不能丢弃的伪造范围"
    elif change == "empty_chapter":
        topic["chapter_id"] = ""
    elif change == "id":
        topic["id"] = "topic06"
    elif change == "trimmed_goal":
        topic["goal"] = " " + topic["goal"] + " "
    else:
        topic.pop("root_axis_ids")
    c, service = topic_coordinator()
    async def forbidden(**kwargs):
        pytest.fail("invalid topic must reject before semantic review")
    c._check_stage_consistency = forbidden
    before = copy.deepcopy((topic, manifest))
    with pytest.raises(BrainstormGenerationError):
        await c.stage_batch(stage="direction", kind="topic_plan", root_request="完整原需求", decisions=[],
            topic=topic, plan={"topics": manifest["topics"]}, limits=limits(), root_limit=4,
            context_id="a" * 64, root_axes_binding=load_root_axes_binding())
    assert service.model_calls == [] and (topic, manifest) == before


@pytest.mark.asyncio
async def test_program_pending_flags_keep_user_original_line_and_still_receive_full_semantic_review():
    edits = {"open_flags": "  已确认读者范围，尚需决定呈现方式  "}
    raw = {"axis_ids": ["purpose"]}
    service = StubCreationService([json.dumps(raw), json.dumps({"checks": {"topic0_goal": "pass", "flag0": "pass"}})])
    c = Coordinator(service)
    root = "用户完整根请求且只使用当前输入"
    skill = {"id": "complete", "skillDescription": {"rules": ["完整规则" * 1500]}, "policy_tail": "末尾原文"}
    result = await c.stage_batch(stage="direction", kind="plan", root_request=root, decisions=[],
        selected_skills=[skill], brief_edits=edits, root_axes_binding=load_root_axes_binding())
    assert result["open_flags"] == [edits["open_flags"]]
    checked = json.loads(service.prompts[1])
    assert checked["validation_task"]["units"][-1]["text"] == edits["open_flags"]
    assert checked["authority"]["user_constraints"] == {
        "root_request": root, "brief_edits": edits, "selected_skills": [skill]}
    assert len(service.model_calls) == 2 and service.model_calls[1]["temperature"] == 0.0
