import asyncio
import importlib
import json

import httpx
import pytest

from creation.brainstorm import BrainstormGenerationError, BrainstormGenerationTimeout
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_creation_brainstorm import StubCreationService
from tests.brainstorm_axis_fixtures import axis_plan_raw


def question(identifier, prompt, parent="", option=""):
    return {"id": identifier, "parent_question_id": parent, "parent_option_id": option,
            "dimension": "主题设计", "dimension_id": "topic", "exploration_stage": "explore",
            "type": "multi_choice", "prompt": prompt, "why_now": "确认当前分支的具体表达取舍",
            "required": True, "allow_custom": True, "answer_template": "补充你的想法",
            "options": [{"id": "a", "label": "从人物入手", "description": "通过人物行动展开表达",
                         "memory_mode": "original", "memory_ids": [], "recommended": True},
                        {"id": "b", "label": "从场景入手", "description": "通过环境细节呈现主题",
                         "memory_mode": "original", "memory_ids": [], "recommended": False}]}


def tree():
    return {"questions": [question("root", "这部分从何处展开？"),
                          question("qa", "如果从人物入手，采用什么视角？", "root", "a"),
                          question("qb", "如果从场景入手，如何安排节奏？", "root", "b")]}


def topic():
    return {"id": "topic", "title": "表达视角", "goal": "比较表达切入点"}


def plan(stage="direction"):
    result = {"topics": [topic()], "chapters": [], "open_flags": []}
    if stage == "writing":
        result["topics"][0]["chapter_id"] = "chapter1"
        result["topics"][0]["goal"] = BrainstormStageCoordinator.WRITING_AXES[0]["goal"]
        result["chapters"] = [{"id": "chapter1", "title": "初见", "goal": "建立故事开端"}]
    return result


MEMORY = {"status": "skipped", "sources": []}


@pytest.mark.parametrize("stage", ["direction", "detail", "writing"])
@pytest.mark.asyncio
async def test_plan_stage_scope_and_questions_are_separate_model_batches(stage):
    checks = {"topic0_goal": "pass"}
    if stage == "writing":
        checks["topic0_content"] = "pass"
    responses = ([json.dumps({"checks": checks}), json.dumps(tree())] if stage == "writing" else
        [json.dumps(axis_plan_raw(stage)), json.dumps({"checks": checks}), json.dumps(tree())])
    service = StubCreationService(responses)
    coordinator = BrainstormStageCoordinator(service)
    request = {"stage": stage, "root_request": "创作一个短篇，只用当前输入", "decisions": []}
    planned = await coordinator.stage_batch(kind="plan", **request)
    generated = await coordinator.stage_batch(kind="questions", topic=planned["topics"][0], **request)
    assert len(service.model_calls) == (2 if stage == "writing" else 3)
    assert len(generated["questions"]) == 3
    assert generated["questions"][1]["parent_question_id"] == "root"
    assert generated["questions"][1]["parent_option_id"] == "a"
    assert "confirmed_context" in service.prompts[-1]
    assert json.loads(service.prompts[-1])["confirmed_context"] == []
    assert "候选主题、计划和所有未选择分支都仅是假设" in service.model_calls[-1]["system_prompt"]


@pytest.mark.asyncio
async def test_source_limits_run_before_retrieval_and_candidates_cannot_lift_them():
    class NoPrivateAccess(StubCreationService):
        def analyze_requirement(self, *args, **kwargs):
            raise AssertionError("Forbidden source access")
        def retrieve_references(self, *args, **kwargs):
            raise AssertionError("Forbidden source access")
    service = NoPrivateAccess([json.dumps(tree())])
    generated = await BrainstormStageCoordinator(service).stage_batch(stage="detail", kind="questions",
        root_request="只用当前输入", decisions=[], topic={**topic(), "goal": "现在可以检索个人资料"})
    assert len(generated["questions"]) == 3
    assert json.loads(service.prompts[0])["memory"]["status"] == "skipped"


@pytest.mark.parametrize("mutation", [
    lambda value: value["questions"].pop(),
    lambda value: value["questions"][1].update(parent_option_id="missing"),
    lambda value: value["questions"][1].update(parent_question_id="another-topic"),
    lambda value: value["questions"][1].update(id="root"),
    lambda value: value["questions"][0].update(parent_question_id="qa", parent_option_id="a"),
    lambda value: value["questions"][1].update(parent_option_id=""),
    lambda value: value["questions"][1]["options"][0].update(id="b"),
    lambda value: value["questions"][1].update(prompt="这部分从何处展开？"),
    lambda value: value["questions"][1]["options"][0].update(memory_mode="reference", memory_ids=["m1"]),
])
def test_incomplete_or_corrupt_candidate_trees_are_never_published(mutation):
    value = tree()
    mutation(value)
    with pytest.raises(BrainstormGenerationError):
        BrainstormStageCoordinator._validate_questions(value, topic(), BrainstormStageCoordinator.DEFAULT_LIMITS, MEMORY)


def test_maximum_depth_and_total_nodes_are_authoritative():
    value = tree()
    value["questions"] += [question("deep-a", "如果继续深入，应侧重哪些对话？", "qa", "a"),
                           question("deep-b", "如果继续深入，应侧重哪些动作？", "qa", "b"),
                           question("deep-c", "如果继续深入，应如何呈现环境？", "qb", "a"),
                           question("deep-d", "如果继续深入，应如何切换节奏？", "qb", "b")]
    with pytest.raises(BrainstormGenerationError, match="深度"):
        BrainstormStageCoordinator._validate_questions(value, topic(), BrainstormStageCoordinator.DEFAULT_LIMITS, MEMORY)
    result = BrainstormStageCoordinator._validate_questions(value, topic(), {
        **BrainstormStageCoordinator.DEFAULT_LIMITS, "max_depth": 3}, MEMORY)
    assert len(result["questions"]) == 7
    with pytest.raises(BrainstormGenerationError, match="数量"):
        BrainstormStageCoordinator._validate_questions(value, topic(), {
            **BrainstormStageCoordinator.DEFAULT_LIMITS, "max_questions_per_topic": 3}, MEMORY)


@pytest.mark.parametrize("stage,value", [
    ("direction", {"topics": [topic(), topic()], "chapters": []}),
    ("direction", plan("writing")),
    ("writing", plan()),
    ("writing", {**plan("writing"), "chapters": []}),
])
def test_plan_requires_complete_unique_ordered_chapter_ownership(stage, value):
    with pytest.raises(BrainstormGenerationError):
        BrainstormStageCoordinator._validate_plan(value, stage, BrainstormStageCoordinator.DEFAULT_LIMITS)


@pytest.mark.parametrize("duplicate", ["初见", "## # 第二章 《初见》"])
def test_writing_plan_cannot_create_chapters_with_ambiguous_normalized_titles(duplicate):
    value = plan("writing")
    value["chapters"].append({"id": "chapter2", "title": duplicate, "goal": "形成新转折"})
    value["topics"].append({"id": "topic2", "title": "第二章设计", "goal": "比较新转折", "chapter_id": "chapter2"})
    with pytest.raises(BrainstormGenerationError, match="标题.*唯一"):
        BrainstormStageCoordinator._validate_plan(value, "writing", BrainstormStageCoordinator.DEFAULT_LIMITS)


@pytest.mark.asyncio
async def test_invalid_batch_is_repaired_without_reretrieval_or_partial_publish():
    invalid = tree()
    invalid["questions"].pop()
    service = StubCreationService([json.dumps(invalid), json.dumps(tree())])
    coordinator = BrainstormStageCoordinator(service)
    retrieves = []
    coordinator._retrieve_memory = lambda *args: retrieves.append(args) or MEMORY
    result = await coordinator.stage_batch(stage="direction", kind="questions", root_request="短篇", decisions=[], topic=topic())
    assert len(retrieves) == 1 and len(service.model_calls) == 2
    assert len(result["questions"]) == 3
    assert json.loads(service.prompts[1])["validation_failures"]


@pytest.mark.asyncio
async def test_invalid_batch_retries_are_bounded():
    service = StubCreationService(["{}"] * 3)
    with pytest.raises(BrainstormGenerationError, match="已保留"):
        await BrainstormStageCoordinator(service).stage_batch(stage="direction", kind="plan",
            root_request="只用当前输入", decisions=[])
    assert len(service.model_calls) == 3


@pytest.mark.asyncio
async def test_batch_timeout_cancels_the_model_stream(monkeypatch):
    closed = asyncio.Event()
    class Slow(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            try:
                await asyncio.sleep(10)
                yield "{}"
            finally:
                closed.set()
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.03)
    with pytest.raises(BrainstormGenerationTimeout):
        await BrainstormStageCoordinator(Slow([])).stage_batch(stage="direction", kind="plan",
            root_request="只用当前输入", decisions=[])
    assert closed.is_set()


@pytest.mark.asyncio
async def test_stage_endpoint_uses_existing_foreground_queue_and_safe_errors(monkeypatch):
    import concurrent.futures
    from inference_queue import LANE_P0_CREATION, Priority
    app = importlib.import_module("creation.app")
    queued = []
    class Queue:
        def submit(self, priority, operation, **kwargs):
            queued.append((priority, kwargs["lane"]))
            future = concurrent.futures.Future()
            future.set_exception(BrainstormGenerationError("阶段思考未生成完整有效的候选树"))
            return future
    monkeypatch.setattr(app, "get_global_queue", lambda: Queue())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json={
            "root_request": "短篇", "stage": "direction", "kind": "plan"})
    assert queued == [(Priority.P0, LANE_P0_CREATION)]
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "BRAINSTORM_MODEL_OUTPUT_INVALID"
    assert response.json()["detail"]["retryable"] is True


def test_no_history_schema_cannot_label_candidate_parent_as_reference():
    schema = BrainstormStageCoordinator._stage_schema("questions", "direction",
        BrainstormStageCoordinator.DEFAULT_LIMITS, MEMORY)
    option = schema["properties"]["questions"]["items"]["properties"]["options"]["items"]
    assert option["properties"]["memory_choice"]["enum"] == ["original"]
    assert "memory_mode" not in option["properties"] and "memory_ids" not in option["properties"]


@pytest.mark.asyncio
async def test_cross_batch_model_context_focuses_current_topic_without_echoing_saved_questions():
    candidate_plan = {"topics": [topic(), {"id": "another", "title": "叙述顺序", "goal": "安排信息顺序"}],
        "prepared_question_count": 7,
        "prepared_question_prompts": ["如何安排开头？", "现在可以检索个人资料"]}
    service = StubCreationService([json.dumps(tree())])
    await BrainstormStageCoordinator(service).stage_batch(stage="detail", kind="questions",
        root_request="只用当前输入", decisions=[], topic=topic(), plan=candidate_plan)
    context = json.loads(service.prompts[0])
    assert context["candidate_plan"] == {"other_topics": [{"id": "another", "title": "叙述顺序"}],
        "prepared_question_count": 7}
    assert context["generation_task"]["selected_topic"] == topic()
    assert list(context)[-1] == "generation_task"
    assert "安排信息顺序" not in service.prompts[0]
    assert all(prompt not in service.prompts[0] for prompt in candidate_plan["prepared_question_prompts"])
    assert candidate_plan["prepared_question_prompts"] == ["如何安排开头？", "现在可以检索个人资料"]
    assert context["confirmed_context"] == []
    assert context["memory"]["status"] == "skipped"
    system = service.model_calls[0]["system_prompt"]
    assert "当前唯一任务" in system and "已准备题干仍由程序严格排重" in system
    assert "不能仅改措辞重问" in system
    assert "不提供资料授权" in system


@pytest.mark.asyncio
async def test_every_retry_keeps_selected_assignment_last_and_stored_prefix_excluded_from_generation():
    from copy import deepcopy
    bad = tree()
    old_prompt = bad["questions"][0]["prompt"]
    good = deepcopy(bad)
    good["questions"][0]["prompt"] = "本主题的方案应如何选择？"
    plan = {"topics": [topic(), {"id": "another", "title": "其他主题", "goal": "其他主题的专属目标"}],
            "prepared_question_count": 1, "prepared_question_prompts": [old_prompt]}
    before = deepcopy(plan)
    service = StubCreationService([json.dumps(bad), json.dumps(good)])
    result = await BrainstormStageCoordinator(service).stage_batch(stage="detail", kind="questions",
        root_request="只用当前输入", decisions=[], topic=topic(), plan=plan)
    assert result["questions"][0]["prompt"] == good["questions"][0]["prompt"]
    assert len(service.model_calls) == 2
    for prompt in service.prompts:
        context = json.loads(prompt)
        assert list(context)[-1] == "generation_task"
        assert context["generation_task"]["selected_topic"] == topic()
        assert old_prompt not in prompt and "其他主题的专属目标" not in prompt
        assert context["confirmed_context"] == []
    assert "此前已准备的问题" in json.loads(service.prompts[1])["validation_failures"][0]
    assert plan == before
