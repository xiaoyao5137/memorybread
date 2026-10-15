"""Overlong stage copy is repaired without regenerating or changing its tree."""

import asyncio
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError, BrainstormGenerationTimeout
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_citations import MEMORY, choice_tree
from tests.test_brainstorm_stages import topic
from tests.test_creation_brainstorm import StubCreationService


MODEL = {"creation_model": None, "creation_api_key": None, "creation_base_url": None}
REPLACEMENTS = {
    "q0_prompt": "这部分从何处展开？",
    "q0_why_now": "确认当前分支的具体表达取舍",
    "q0_o0_label": "从人物行动入手",
    "q1_o1_description": "通过环境细节呈现主题",
}


def long_tree():
    candidate = choice_tree("reference:m1+m2")
    candidate["questions"][0]["prompt"] = "当前这部分的主题表达需要确定怎样的展开方向？" * 4
    candidate["questions"][0]["why_now"] = "确认当前分支的具体表达取舍，比较人物行动与环境细节的作用。" * 4
    candidate["questions"][0]["options"][0]["label"] = "从人物行动入手逐步形成故事结构与人物情感变化的完整表达"
    candidate["questions"][1]["options"][1]["description"] = "通过环境细节呈现主题，同时保留人物处境与当前场景的关联。" * 4
    return candidate


def repair_response(replacements=None):
    return json.dumps({"replacements": dict(REPLACEMENTS if replacements is None else replacements)},
                      ensure_ascii=False)


def coordinator_with(responses):
    service = StubCreationService(responses)
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(MEMORY)
    return coordinator, service


def expected_repaired(candidate):
    expected = copy.deepcopy(candidate)
    expected["questions"][0]["prompt"] = REPLACEMENTS["q0_prompt"]
    expected["questions"][0]["why_now"] = REPLACEMENTS["q0_why_now"]
    expected["questions"][0]["options"][0]["label"] = REPLACEMENTS["q0_o0_label"]
    expected["questions"][1]["options"][1]["description"] = REPLACEMENTS["q1_o1_description"]
    return expected


@pytest.mark.asyncio
async def test_all_overlong_fields_are_atomically_repaired_with_tree_and_sources_unchanged():
    candidate = long_tree()
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([repair_response()])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert candidate == before
    assert repaired == expected_repaired(before)
    assert len(service.model_calls) == 1
    context = json.loads(service.prompts[0])
    assert set(context["fields"]) == set(REPLACEMENTS)
    assert context["root_request"] == "构思人物故事"
    assert context["candidate_topic"] == topic()
    assert context["candidate_questions"][0]["question"] == before["questions"][0]
    schema = service.model_calls[0]["json_schema"]
    assert schema["additionalProperties"] is False
    replacement_schema = schema["properties"]["replacements"]
    assert replacement_schema["additionalProperties"] is False
    assert set(replacement_schema["required"]) == set(REPLACEMENTS)
    assert replacement_schema["properties"]["q0_prompt"]["maxLength"] == 60
    assert replacement_schema["properties"]["q0_why_now"]["maxLength"] == 80
    assert replacement_schema["properties"]["q0_o0_label"]["maxLength"] == 24
    assert replacement_schema["properties"]["q1_o1_description"]["maxLength"] == 80
    assert 1200 <= service.model_calls[0]["num_predict"] <= 2400
    result = coordinator._validate_questions(repaired, topic(), coordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))
    assert MEMORY["sources"][0]["content"] in result["questions"][0]["options"][0]["details"]
    assert MEMORY["sources"][1]["content"] in result["memory_brief"]


@pytest.mark.asyncio
async def test_stage_generates_one_tree_and_one_small_repair_instead_of_another_tree():
    coordinator, service = coordinator_with([json.dumps(long_tree(), ensure_ascii=False), repair_response()])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(result["questions"]) == 3
    assert result["questions"][0]["prompt"] == REPLACEMENTS["q0_prompt"]
    assert result["questions"][1]["parent_question_id"] == "root"
    assert result["questions"][1]["parent_option_id"] == "a"
    assert len(service.model_calls) == 2
    assert service.model_calls[0]["num_predict"] == 8000
    assert service.model_calls[1]["num_predict"] <= 2400


@pytest.mark.asyncio
async def test_normal_candidate_uses_no_copy_repair_call():
    coordinator, service = coordinator_with([json.dumps(choice_tree(), ensure_ascii=False)])
    result = await coordinator.stage_batch(stage="direction", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(result["questions"]) == 3
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_copy_repair_does_not_invent_missing_empty_or_wrongly_typed_fields():
    candidate = choice_tree()
    candidate["questions"][0]["prompt"] = None
    candidate["questions"][0]["why_now"] = []
    candidate["questions"][0]["options"][0]["label"] = ""
    candidate["questions"][0]["options"][0].pop("description")
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([])
    assert await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL) == before
    assert service.model_calls == []


@pytest.mark.parametrize("response", [
    repair_response({}),
    repair_response({**REPLACEMENTS, "unexpected": "新增内容"}),
    repair_response({**REPLACEMENTS, "q0_id": "injected_id"}),
    repair_response({**REPLACEMENTS, "q0_o0_memory_choice": "original"}),
    repair_response({**REPLACEMENTS, "q0_o0_label": "超" * 25}),
    repair_response({**REPLACEMENTS, "q0_prompt": ""}),
    repair_response({**REPLACEMENTS, "q0_why_now": "   "}),
    repair_response({**REPLACEMENTS, "q0_prompt": None}),
    json.dumps({"replacements": REPLACEMENTS, "questions": []}),
    json.dumps({"replacements": []}),
    "{}",
    "invalid JSON",
])
@pytest.mark.asyncio
async def test_invalid_replacements_are_bounded_and_leave_the_original_candidate_intact(response, caplog):
    candidate = long_tree()
    candidate["questions"][0]["prompt"] += "PRIVATE_FIELD_CONTENT"
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([response, response])
    with pytest.raises(BrainstormGenerationError, match="候选树未发布"):
        await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert candidate == before
    assert len(service.model_calls) == 2
    assert "PRIVATE_FIELD_CONTENT" not in caplog.text
    assert "validation_failures" in json.loads(service.prompts[1])


@pytest.mark.asyncio
async def test_second_small_repair_can_recover_without_replacing_the_candidate_tree():
    candidate = long_tree()
    coordinator, service = coordinator_with([repair_response({}), repair_response()])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert repaired == expected_repaired(candidate)
    assert len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_large_copy_schema_is_rejected_before_decoding_limits_could_expand_unboundedly():
    candidate = {"questions": []}
    for batch_index in range(6):
        batch = long_tree()
        for question in batch["questions"]:
            question["id"] = "batch{}_{}".format(batch_index, question["id"])
            if question["parent_question_id"]:
                question["parent_question_id"] = "batch{}_{}".format(batch_index, question["parent_question_id"])
            question["prompt"] += "第{}组".format(batch_index)
        candidate["questions"].extend(batch["questions"])
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([])
    with pytest.raises(BrainstormGenerationError, match="短文案预算"):
        await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert service.model_calls == []
    assert candidate == before


@pytest.mark.asyncio
async def test_length_repair_feedback_contains_only_controlled_path_and_counts(caplog):
    candidate = long_tree()
    bad = repair_response({**REPLACEMENTS, "q0_o0_label": "PRIVATE_MODEL_TEXT" * 4})
    coordinator, service = coordinator_with([bad, repair_response()])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert repaired == expected_repaired(candidate)
    assert "q0_o0_label 长度 72，要求 1 至 24" in caplog.text
    assert "PRIVATE_MODEL_TEXT" not in caplog.text
    assert "q0_o0_label" in json.loads(service.prompts[1])["validation_failures"][0]


@pytest.mark.parametrize("corruption", ["graph", "source"])
@pytest.mark.asyncio
async def test_copy_repair_does_not_bypass_graph_or_source_validation(corruption):
    candidate = long_tree()
    if corruption == "graph":
        candidate["questions"][1]["parent_question_id"] = "missing_parent"
    else:
        candidate["questions"][0]["options"][0]["memory_choice"] = "reference:m99"
    coordinator, _service = coordinator_with([repair_response()])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    with pytest.raises(BrainstormGenerationError):
        coordinator._validate_questions(repaired, topic(), coordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))


@pytest.mark.asyncio
async def test_copy_repair_is_cancelled_by_the_existing_stage_deadline(monkeypatch):
    closed = asyncio.Event()
    candidate = long_tree()
    before = copy.deepcopy(candidate)

    class SlowRepair(StubCreationService):
        async def _stream_direct_completion(self, **kwargs):
            self.model_calls.append(kwargs)
            self.prompts.append(kwargs["user_prompt"])
            if len(self.model_calls) == 1:
                yield json.dumps(candidate, ensure_ascii=False)
                return
            try:
                await asyncio.sleep(10)
                yield repair_response()
            finally:
                closed.set()

    service = SlowRepair([])
    coordinator = BrainstormStageCoordinator(service)
    coordinator._retrieve_memory = lambda *args: copy.deepcopy(MEMORY)
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.03)
    with pytest.raises(BrainstormGenerationTimeout):
        await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
            decisions=[], topic=topic())
    assert closed.is_set()
    assert candidate == before
    assert len(service.model_calls) == 2


@pytest.mark.parametrize("questions", [None, {}, "invalid", []])
@pytest.mark.asyncio
async def test_invalid_question_collections_do_not_start_copy_repair(questions):
    response = json.dumps({"questions": questions})
    coordinator, service = coordinator_with([response] * coordinator_attempt_count())
    with pytest.raises(BrainstormGenerationError):
        await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
            decisions=[], topic=topic())
    assert len(service.model_calls) == coordinator.MAX_GENERATION_ATTEMPTS
    assert all(call["num_predict"] == 8000 for call in service.model_calls)


def coordinator_attempt_count():
    return BrainstormStageCoordinator.MAX_GENERATION_ATTEMPTS


@pytest.mark.asyncio
async def test_too_many_questions_are_rejected_before_any_small_copy_call():
    candidate = long_tree()
    candidate["questions"] *= 5
    coordinator, service = coordinator_with([json.dumps(candidate)] * coordinator_attempt_count())
    with pytest.raises(BrainstormGenerationError):
        await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
            decisions=[], topic=topic())
    assert len(service.model_calls) == coordinator.MAX_GENERATION_ATTEMPTS
    assert all("questions" in call["json_schema"]["properties"] for call in service.model_calls)


@pytest.mark.asyncio
async def test_previous_batch_question_fingerprints_reject_reworded_punctuation_and_retry():
    duplicate = choice_tree()
    duplicate["questions"][0]["prompt"] = "这 部 分，从何处展开!"
    replacement = choice_tree()
    replacement["questions"][0]["prompt"] = "这部分由哪个人物带入？"
    coordinator, service = coordinator_with([json.dumps(duplicate), json.dumps(replacement)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic(), plan={"prepared_question_prompts": ["这部分从何处展开？"]})
    assert result["questions"][0]["prompt"] == replacement["questions"][0]["prompt"]
    assert len(service.model_calls) == 2
    assert "此前已准备的问题" in service.prompts[1]


@pytest.mark.parametrize("invalid_question_index", [0, 2])
@pytest.mark.asyncio
async def test_invalid_original_citations_are_rejected_before_copy_can_erase_them(invalid_question_index):
    candidate = long_tree()
    candidate["questions"][invalid_question_index]["options"][1]["description"] = "根据记忆 m99，采用场景细节呈现主题。" * 8
    coordinator, service = coordinator_with([json.dumps(candidate)] * coordinator_attempt_count())
    with pytest.raises(BrainstormGenerationError):
        await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
            decisions=[], topic=topic())
    assert len(service.model_calls) == coordinator.MAX_GENERATION_ATTEMPTS
    assert all("questions" in call["json_schema"]["properties"] for call in service.model_calls)


@pytest.mark.asyncio
async def test_valid_legacy_inline_reference_survives_shortening_its_description():
    candidate = choice_tree()
    option = candidate["questions"][0]["options"][0]
    option.pop("memory_choice")
    option.update(memory_mode="reference", memory_ids=[],
        description="根据 m1 的资料，通过人物行动展开表达，同时利用场景细节说明人物处境。" * 4)
    before = copy.deepcopy(candidate)
    response = json.dumps({"replacements": {"q0_o0_description": "通过人物行动展开表达。"}}, ensure_ascii=False)
    coordinator, service = coordinator_with([json.dumps(candidate, ensure_ascii=False), response])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert candidate == before
    assert len(service.model_calls) == 2
    context = json.loads(service.prompts[1])
    verified = context["candidate_questions"][0]["question"]["options"][0]["memory_evidence"]
    assert verified == [{"memory_id": "m1", "quote": MEMORY["sources"][0]["content"]}]
    assert MEMORY["sources"][0]["content"] in result["questions"][0]["options"][0]["details"]
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]


@pytest.mark.asyncio
async def test_compression_label_collision_recovers_with_second_small_call_and_only_one_tree_call():
    collision = {**REPLACEMENTS, "q0_o0_label": "从场景入手"}
    coordinator, service = coordinator_with([json.dumps(long_tree()), repair_response(collision), repair_response()])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(service.model_calls) == 3
    assert service.model_calls[0]["num_predict"] == 8000
    assert all(call["num_predict"] <= 2400 for call in service.model_calls[1:])
    feedback = json.loads(service.prompts[2])
    assert "q0_o0_label" in feedback["validation_failures"][0]
    assert feedback["rejected_replacements"] == collision
    assert result["questions"][0]["options"][0]["label"] == REPLACEMENTS["q0_o0_label"]
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]


@pytest.mark.asyncio
async def test_distinct_existing_descriptions_allow_duplicate_option_names_to_be_clarified():
    candidate = choice_tree("reference:m1+m2")
    candidate["questions"][0]["options"][1]["label"] = candidate["questions"][0]["options"][0]["label"]
    response = repair_response({"q0_o0_label": "通过人物行动展开", "q0_o1_label": "通过环境细节展开"})
    coordinator, service = coordinator_with([json.dumps(candidate), response])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(service.model_calls) == 2
    assert [option["label"] for option in result["questions"][0]["options"]] == [
        "通过人物行动展开", "通过环境细节展开"]
    assert MEMORY["sources"][1]["content"] in result["memory_brief"]


@pytest.mark.parametrize("reason", ["identical_options", "duplicate_roots", "same_condition_path", "empty_condition"])
@pytest.mark.asyncio
async def test_actual_duplicate_choices_or_decisions_are_not_repaired_into_new_meanings(reason):
    candidate = choice_tree()
    if reason == "identical_options":
        first, second = candidate["questions"][0]["options"]
        second.update(label=first["label"], description=first["description"])
    elif reason == "duplicate_roots":
        candidate["questions"][1].update(parent_question_id="", parent_option_id="",
            prompt=candidate["questions"][0]["prompt"])
    elif reason == "same_condition_path":
        candidate["questions"][2].update(parent_option_id="a", prompt=candidate["questions"][1]["prompt"])
    else:
        candidate["questions"][0]["options"][0]["label"] = "..."
        candidate["questions"][2]["prompt"] = candidate["questions"][1]["prompt"]
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([])
    with pytest.raises(BrainstormGenerationError):
        await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert candidate == before
    assert service.model_calls == []


@pytest.mark.asyncio
async def test_identical_child_prompts_are_restated_using_their_distinct_parent_options():
    candidate = choice_tree("reference:m1+m2")
    candidate["questions"][1]["prompt"] = "这个方向如何组织表达？"
    candidate["questions"][2]["prompt"] = "这个方向如何组织表达？"
    replacements = {"q1_prompt": "如果从人物入手，采用什么视角？",
                    "q2_prompt": "如果从场景入手，如何安排节奏？"}
    coordinator, service = coordinator_with([json.dumps(candidate), repair_response(replacements)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(service.model_calls) == 2
    assert result["questions"][1]["prompt"] == replacements["q1_prompt"]
    assert result["questions"][2]["prompt"] == replacements["q2_prompt"]
    assert result["questions"][1]["parent_option_id"] == "a"
    assert result["questions"][2]["parent_option_id"] == "b"
    context = json.loads(service.prompts[1])
    assert context["fields"]["q1_prompt"]["conditions"][-1]["option_label"] == "从人物入手"
    assert context["fields"]["q2_prompt"]["conditions"][-1]["option_label"] == "从场景入手"
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]


@pytest.mark.asyncio
async def test_unique_child_wording_must_still_preserve_its_declared_parent_condition():
    candidate = choice_tree()
    candidate["questions"][1]["prompt"] = "这个方向如何组织表达？"
    candidate["questions"][2]["prompt"] = "这个方向如何组织表达？"
    first = {"q1_prompt": "采用什么视角推进人物？", "q2_prompt": "怎样安排环境节奏？"}
    second = {"q1_prompt": "如果从人物入手，采用什么视角？",
              "q2_prompt": "如果从场景入手，如何安排节奏？"}
    coordinator, service = coordinator_with([json.dumps(candidate), repair_response(first), repair_response(second)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(service.model_calls) == 3
    feedback = json.loads(service.prompts[2])
    assert "父选项条件" in feedback["validation_failures"][0]
    assert feedback["rejected_replacements"] == first
    assert result["questions"][1]["prompt"] == second["q1_prompt"]


@pytest.mark.asyncio
async def test_parent_label_and_child_condition_can_be_coordinated_in_the_same_copy_call():
    candidate = choice_tree()
    parent_label = "从人物行动入手逐步形成故事结构与人物情感变化的完整表达"
    candidate["questions"][0]["options"][0]["label"] = parent_label
    candidate["questions"][1]["prompt"] = "这个方向如何组织表达？"
    candidate["questions"][2]["prompt"] = "这个方向如何组织表达？"
    first = {"q0_o0_label": "人物行动推进", "q1_prompt": "如果" + parent_label + "，如何安排视角？",
             "q2_prompt": "如果从场景入手，如何安排节奏？"}
    second = {**first, "q1_prompt": "若采用人物行动推进，如何安排视角？"}
    coordinator, service = coordinator_with([json.dumps(candidate), repair_response(first), repair_response(second)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert len(service.model_calls) == 3
    feedback = json.loads(service.prompts[2])
    assert feedback["rejected_replacements"] == first
    assert "父选项条件" in feedback["validation_failures"][0]
    assert result["questions"][0]["options"][0]["label"] == "人物行动推进"
    assert result["questions"][1]["prompt"] == second["q1_prompt"]


@pytest.mark.asyncio
async def test_compression_collision_with_prepared_question_only_retries_small_copy():
    alternative = {**REPLACEMENTS, "q0_prompt": "这部分优先通过什么方式展开？"}
    coordinator, service = coordinator_with([json.dumps(long_tree()), repair_response(), repair_response(alternative)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic(), plan={"prepared_question_prompts": [REPLACEMENTS["q0_prompt"]]})
    assert len(service.model_calls) == 3
    assert result["questions"][0]["prompt"] == alternative["q0_prompt"]
    assert "此前已准备的问题" in json.loads(service.prompts[2])["validation_failures"][0]


@pytest.mark.asyncio
async def test_original_prepared_question_duplicate_is_never_sent_for_copy_rephrasing():
    candidate = choice_tree()
    coordinator, service = coordinator_with([])
    with pytest.raises(BrainstormGenerationError, match="不能仅改写题干"):
        await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(),
            existing_question_prompts=[candidate["questions"][0]["prompt"]], **MODEL)
    assert service.model_calls == []


@pytest.mark.parametrize("broken,reason", [
    ("截至2026-08-", "日期片段缺少日数字"),
    ("2026-08-期间的变化", "日期片段缺少日数字"),
    ("通过人物行动与", "末尾连接词缺少后续内容"),
    ("通过人物行动以及。", "末尾连接词缺少后续内容"),
    ("通过人物行动并且”,", "末尾连接词缺少后续内容"),
    ("通过人物行动或者", "末尾连接词缺少后续内容"),
    ("计划范围2026-08-17至", "末尾连接词缺少后续内容"),
    ("计划范围2026年8月17日至", "末尾连接词缺少后续内容"),
    ("利用率25%至。", "末尾连接词缺少后续内容"),
    ("数量１至", "末尾连接词缺少后续内容"),
    ("先总后分：先展示整体收益，再列前", "末尾序数缺少数量或对象"),
    ("随后列出第。", "末尾序数缺少数量或对象"),
    ("先展示前", "末尾序数缺少数量或对象"),
])
def test_copy_completeness_guard_recognizes_only_clear_fragments(broken, reason):
    assert BrainstormStageCoordinator._copy_completeness_issue(broken) == reason


@pytest.mark.parametrize("label", [
    "GPU", "AIGC", "AIGC Loop Agent", "2026", "2026-08",
    "2026-08-17", "2026-08-17至2026-08-23", "2026-08-alpha",
    "Weekly GPU ROI", "成本与利用率", "指标总和", "知识普及", "用户参与", "无微不至",
    "夏至", "冬至", "节气范围截至冬至", "资产赠与", "荣誉授与", "价值付与", "善意施与", "友人相与", "机会给与",
])
@pytest.mark.asyncio
async def test_valid_acronyms_dates_english_and_short_titles_need_no_copy_call(label):
    candidate = choice_tree("reference:m1+m2")
    candidate["questions"][0]["options"][0]["label"] = label
    coordinator, service = coordinator_with([json.dumps(candidate)])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert BrainstormStageCoordinator._copy_completeness_issue(label) is None
    assert len(service.model_calls) == 1
    assert result["questions"][0]["options"][0]["label"] == label
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]


@pytest.mark.parametrize("broken,complete", [
    ("截至2026-08-", "截至2026-08-17的人物行动"),
    ("通过人物行动与", "通过人物行动展开"),
])
@pytest.mark.asyncio
async def test_already_short_broken_label_enters_targeted_repair_without_other_changes(broken, complete):
    candidate = choice_tree("reference:m1+m2")
    option = candidate["questions"][0]["options"][0]
    option["label"] = broken
    option["description"] = "以2026-08-17之前的人物行动展开表达"
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([repair_response({"q0_o0_label": complete})])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    expected = copy.deepcopy(before)
    expected["questions"][0]["options"][0]["label"] = complete
    assert repaired == expected
    assert candidate == before
    assert len(service.model_calls) == 1
    context = json.loads(service.prompts[0])
    assert set(context["fields"]) == {"q0_o0_label"}
    assert context["fields"]["q0_o0_label"]["completeness_issue"]
    result = coordinator._validate_questions(repaired, topic(), coordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]
    assert MEMORY["sources"][1]["content"] in result["memory_brief"]


@pytest.mark.parametrize("broken", ["截至2026-08-", "通过人物行动与"])
@pytest.mark.asyncio
async def test_compression_fragment_retries_only_small_copy_then_preserves_tree_and_sources(broken, caplog):
    invalid = {**REPLACEMENTS, "q0_o0_label": broken}
    candidate = long_tree()
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([json.dumps(candidate), repair_response(invalid), repair_response()])
    result = await coordinator.stage_batch(stage="detail", kind="questions", root_request="构思人物故事",
        decisions=[], topic=topic())
    assert candidate == before
    assert len(service.model_calls) == 3
    assert service.model_calls[0]["num_predict"] == 8000
    assert all(call["num_predict"] <= 2400 for call in service.model_calls[1:])
    feedback = json.loads(service.prompts[2])
    assert "q0_o0_label 不完整" in feedback["validation_failures"][0]
    assert feedback["rejected_replacements"] == invalid
    assert broken not in caplog.text
    assert result["questions"][0]["options"][0]["label"] == REPLACEMENTS["q0_o0_label"]
    assert result["questions"][1]["parent_question_id"] == "root"
    assert result["questions"][1]["parent_option_id"] == "a"
    assert MEMORY["sources"][0]["content"] in result["memory_brief"]
    assert MEMORY["sources"][1]["content"] in result["memory_brief"]


@pytest.mark.parametrize("field", ["prompt", "why_now", "label", "description"])
def test_full_batch_validation_rejects_short_fragments_even_without_copy_targets(field):
    candidate = choice_tree()
    destination = candidate["questions"][0]
    if field in {"label", "description"}:
        destination = destination["options"][0]
    destination[field] = "展示人物行动与"
    with pytest.raises(BrainstormGenerationError, match="不完整"):
        BrainstormStageCoordinator._validate_questions(candidate, topic(),
            BrainstormStageCoordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))


@pytest.mark.asyncio
async def test_two_incomplete_copy_responses_leave_original_candidate_unpublished():
    candidate = choice_tree()
    candidate["questions"][0]["options"][0]["label"] = "展示人物行动与"
    before = copy.deepcopy(candidate)
    broken = repair_response({"q0_o0_label": "人物变化并且"})
    coordinator, service = coordinator_with([broken, broken])
    with pytest.raises(BrainstormGenerationError, match="候选树未发布"):
        await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert candidate == before
    assert len(service.model_calls) == 2


@pytest.mark.asyncio
async def test_each_small_copy_prompt_ends_on_current_field_task_after_rejected_text():
    candidate = long_tree()
    invalid = {**REPLACEMENTS, "q0_o0_label": "截至2026-08-"}
    coordinator, service = coordinator_with([repair_response(invalid), repair_response()])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert repaired == expected_repaired(candidate)
    assert len(service.model_calls) == 2
    assert coordinator.MAX_COPY_REPAIR_ATTEMPTS == 2
    for prompt in service.prompts:
        context = json.loads(prompt)
        keys = list(context)
        assert keys[-1] == "repair_task"
        assert keys.index("rejected_replacements") < keys.index("fields") < keys.index("repair_task")
        task = context["repair_task"]
        assert set(task["field_paths"]) == set(REPLACEMENTS)
        assert task["max_chars"] == {"q0_prompt": 60, "q0_why_now": 80,
                                     "q0_o0_label": 24, "q1_o1_description": 80}
        assert "严禁" in task["failure_policy"] and "前缀" in task["failure_policy"]
        assert "共有年/月" in task["date_ranges"] and "不可猜测" in task["date_ranges"]
        assert "不能修改其他字段" in task["task"]
    second_context = json.loads(service.prompts[1])
    assert second_context["rejected_replacements"] == invalid
    assert "不完整" in second_context["validation_failures"][0]
    assert all(1200 <= call["num_predict"] <= 2400 for call in service.model_calls)


@pytest.mark.parametrize("complete_label", ["限定2026-08-17至08-23", "严格限定上述完整统计范围"])
@pytest.mark.asyncio
async def test_long_date_range_can_use_complete_short_label_with_original_dates_retained(complete_label):
    candidate = choice_tree("reference:m1+m2")
    option = candidate["questions"][0]["options"][0]
    option["label"] = "严格限定统计范围为2026-08-17至2026-08-23"
    option["description"] = "完整统计范围为2026-08-17至2026-08-23，比较人物行动的展开表达"
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([repair_response({"q0_o0_label": complete_label})])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    expected = copy.deepcopy(before)
    expected["questions"][0]["options"][0]["label"] = complete_label
    assert repaired == expected
    assert candidate == before
    assert len(service.model_calls) == 1
    assert set(json.loads(service.prompts[0])["repair_task"]["field_paths"]) == {"q0_o0_label"}
    result = coordinator._validate_questions(repaired, topic(), coordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))
    normalized_option = result["questions"][0]["options"][0]
    assert normalized_option["description"] == option["description"]
    assert "2026-08-17至2026-08-23" in normalized_option["description"]
    details = normalized_option["details"]
    assert MEMORY["sources"][0]["content"] in details
    assert MEMORY["sources"][1]["content"] in details


@pytest.mark.asyncio
async def test_copy_concision_guide_is_advisory_and_preserves_exact_decoder_and_business_budgets():
    candidate = choice_tree("reference:m1+m2")
    candidate["questions"][0]["options"][0]["label"] = "通过完整呈现 AIGC Loop Agent 的人物行动安排来组织故事表达"
    concise_label = "AIGC Loop Agent人物行动"
    assert 16 < len(concise_label) <= 24
    before = copy.deepcopy(candidate)
    coordinator, service = coordinator_with([repair_response({"q0_o0_label": concise_label})])
    repaired = await coordinator._repair_question_copy(candidate, root_request="构思人物故事", topic=topic(), **MODEL)
    assert candidate == before
    assert repaired["questions"][0]["options"][0]["label"] == concise_label
    assert len(service.model_calls) == 1
    call = service.model_calls[0]
    system = call["system_prompt"]
    assert "先重组措辞再输出" in system
    assert "12 至 16" in system and "不是硬性长度限制" in system
    assert "专名和必需条件优先保留" in system
    assert "完整范围含义表达" in system
    assert "不得改变原说明或引用中的日期、数值" in system
    assert "示例日期不是当前事实，不可照用" in system
    assert system.rstrip().endswith("不能截断或用残句满足长度。")
    schema = call["json_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["replacements"]["properties"]["q0_o0_label"] == {
        "type": "string", "minLength": 1, "maxLength": 24}
    assert json.loads(service.prompts[0])["repair_task"]["max_chars"] == {"q0_o0_label": 24}
    assert coordinator.MAX_COPY_REPAIR_ATTEMPTS == 2
    assert coordinator.MAX_COPY_REPAIR_SCHEMA_CHARS == 1200
    assert 1200 <= call["num_predict"] <= 2400


@pytest.mark.parametrize("label", ["先展示前10项再解释依据", "原行定位置于展示前", "随后列出第一项结果"])
def test_complete_ordinals_and_temporal_positions_are_preserved(label):
    assert BrainstormStageCoordinator._copy_completeness_issue(label) is None
