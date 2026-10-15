"""Repair enumerated answers through the unchanged question validation rule."""

import copy
import json

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_copy_repair import MODEL, coordinator_with, repair_response
from tests.test_brainstorm_stage_citations import MEMORY, choice_tree
from tests.test_brainstorm_stage_nodes import candidate, node, prepare, setup
from tests.test_brainstorm_stages import topic


@pytest.mark.parametrize("prompt,labels,expected", [
    ("选 Alpha 或 BRAVO？", ["AL pha", "bravo"], {"alpha", "bravo"}),
    ("从人物入手还是从场景入手？", ["从人物入手", "从场景入手"], {"从人物入手", "从场景入手"}),
    ("选甲还是乙？", ["甲", "乙"], set()),
    ("ABCD ABCD？", ["ABCD", "ab cd"], {"abcd"}),
    ("针对从人物入手，如何安排节奏？", ["从人物入手", "从场景入手"], {"从人物入手"}),
    (None, ["从人物入手", "从场景入手"], set()),
])
def test_shared_predicate_preserves_whitespace_case_minimum_and_distinct_count(prompt, labels, expected):
    options = [{"label": label} for label in labels]
    assert BrainstormCoordinator._prompt_repeated_option_labels(prompt, options) == expected
    assert BrainstormStageCoordinator._prompt_repeated_option_labels(prompt, options) == expected


def enumerating_tree():
    value = choice_tree("reference:m1+m2")
    value["questions"][0]["prompt"] = "这部分从人物入手还是从场景入手？"
    return value


@pytest.mark.asyncio
async def test_enum_issue_targets_only_prompt_and_preserves_complete_tree_and_sources():
    value = enumerating_tree()
    before = copy.deepcopy(value)
    replacement = "这部分从何处展开？"
    coordinator, service = coordinator_with([repair_response({"q0_prompt": replacement})])
    result = await coordinator._repair_question_copy(value, root_request="构思人物故事", topic=topic(), **MODEL)
    expected = copy.deepcopy(before)
    expected["questions"][0]["prompt"] = replacement
    assert result == expected and value == before
    context = json.loads(service.prompts[0])
    assert context["fields"] == {"q0_prompt": {
        "text": before["questions"][0]["prompt"], "max_chars": 60, "prompt_option_enumeration": True}}
    assert context["candidate_questions"][0]["question"] == before["questions"][0]
    assert set(service.model_calls[0]["json_schema"]["properties"]["replacements"]["properties"]) == {"q0_prompt"}
    assert service.model_calls[0]["num_predict"] == 1200
    validated = coordinator._validate_questions(result, topic(), coordinator.DEFAULT_LIMITS, copy.deepcopy(MEMORY))
    assert MEMORY["sources"][0]["content"] in validated["questions"][0]["options"][0]["details"]
    assert validated["questions"][1]["parent_option_id"] == "a"
    assert validated["questions"][2]["parent_option_id"] == "b"


@pytest.mark.asyncio
async def test_replacement_that_still_enumerates_is_retried_by_same_predicate():
    value = enumerating_tree()
    before = copy.deepcopy(value)
    coordinator, service = coordinator_with([
        repair_response({"q0_prompt": "从场景入手还是从人物入手？"}),
        repair_response({"q0_prompt": "这部分从何处展开？"}),
    ])
    result = await coordinator._repair_question_copy(value, root_request="构思人物故事", topic=topic(), **MODEL)
    assert value == before and len(service.model_calls) == 2
    assert result["questions"][0]["prompt"] == "这部分从何处展开？"
    assert "重复列举多个选项" in json.loads(service.prompts[1])["validation_failures"][0]


@pytest.mark.asyncio
async def test_two_bad_replacements_fail_without_program_removing_any_copy():
    value = enumerating_tree()
    before = copy.deepcopy(value)
    response = repair_response({"q0_prompt": "从场景入手还是从人物入手？"})
    coordinator, service = coordinator_with([response, response])
    with pytest.raises(BrainstormGenerationError, match="候选树未发布"):
        await coordinator._repair_question_copy(value, root_request="构思人物故事", topic=topic(), **MODEL)
    assert value == before and len(service.model_calls) == coordinator.MAX_COPY_REPAIR_ATTEMPTS == 2


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.asyncio
async def test_grouped_provider_repairs_only_bound_body_then_runs_complete_node_gate(depth):
    request_node = node(depth=depth)
    value = candidate(depth=depth, choice="reference:m1+m2")
    value["question"]["prompt_body"] = "从人物入手还是从场景入手？"
    neutral_body = "这部分从何处展开？" if depth == 1 else "当前分支从何处展开？"
    coordinator, service = setup([value, {"replacements": {"q0_prompt": neutral_body}}], MEMORY)
    semantic_inputs = []

    async def capture_semantic(**kwargs):
        semantic_inputs.append(copy.deepcopy(kwargs["result"]))

    coordinator._check_stage_consistency = capture_semantic
    result = await prepare(coordinator, node=request_node, stage="direction")
    prefix = coordinator._node_condition_prefix(request_node)
    assert result["question"]["prompt"] == prefix + neutral_body
    assert len(service.model_calls) == 2 and len(semantic_inputs) == 1
    assert semantic_inputs[0] == result
    context = json.loads(service.prompts[1])
    assert context["fields"] == {"q0_prompt": {
        "text": value["question"]["prompt_body"], "max_chars": 60 - len(prefix),
        "bound_condition_prefix": prefix, "prompt_option_enumeration": True}}
    assert [option["label"] for option in result["question"]["options"]] == [
        option["label"] for option in value["question"]["options"]]
    assert result["next_goals"] == ([] if depth == 2 else [
        {"option_id": "a", "goals": ["比較人物视角", "安排人物出场"]},
        {"option_id": "b", "goals": ["比较场景节奏"]},
    ])
    assert result["question"]["parent_question_id"] == request_node["parent_question_id"]
    assert result["question"]["parent_option_id"] == request_node["parent_option_id"]
    assert MEMORY["sources"][0]["content"] in result["question"]["options"][0]["details"]
    assert MEMORY["sources"][1]["content"] in result["question"]["options"][0]["details"]


@pytest.mark.asyncio
async def test_three_provider_branches_and_recommended_mapping_survive_body_repair():
    value = candidate(choice="reference:m1+m2")
    value["question"]["prompt_body"] = "从人物入手、从场景入手还是从事件入手？"
    value["question"]["options"][0]["recommended"] = False
    value["question"]["options"].append({"label": "从事件入手", "description": "通过事件次序展开表达",
        "recommended": True, "memory_choice": "original"})
    value["next_goals"].append({"option_index": 3, "goal": "安排事件顺序"})
    request_node = {**node(), "max_options": 3, "max_next_goals": 5}
    coordinator, service = setup([value, {"replacements": {"q0_prompt": "这部分从何处展开？"}}], MEMORY)
    result = await prepare(coordinator, node=request_node)
    assert [option["id"] for option in result["question"]["options"]] == ["c", "a", "b"]
    assert result["next_goals"] == [
        {"option_id": "a", "goals": ["比較人物视角", "安排人物出场"]},
        {"option_id": "b", "goals": ["比较场景节奏"]},
        {"option_id": "c", "goals": ["安排事件顺序"]},
    ]
    assert set(json.loads(service.prompts[1])["fields"]) == {"q0_prompt"}


@pytest.mark.asyncio
async def test_prefix_caused_repetition_is_not_exempted_or_rewritten():
    prefix = "如果从人物入手且从场景入手，"
    value = {"questions": [choice_tree()["questions"][0]]}
    value["questions"][0]["prompt"] = prefix + "这部分从何处展开？"
    before = copy.deepcopy(value)
    response = repair_response({"q0_prompt": "当前分支采用什么视角？"})
    coordinator, service = coordinator_with([response, response])
    with pytest.raises(BrainstormGenerationError, match="候选树未发布"):
        await coordinator._repair_question_copy(value, root_request="构思人物故事", topic=topic(),
            bound_prompt_prefix=prefix, **MODEL)
    assert value == before and len(service.model_calls) == 2
    for prompt in service.prompts:
        assert json.loads(prompt)["fields"]["q0_prompt"]["bound_condition_prefix"] == prefix


@pytest.mark.asyncio
async def test_one_label_context_preserved_without_starting_repair():
    value = choice_tree()
    value["questions"][0]["prompt"] = "针对从人物入手，采用什么视角？"
    coordinator, service = coordinator_with([])
    assert await coordinator._repair_question_copy(value, root_request="构思人物故事", topic=topic(), **MODEL) is value
    assert service.model_calls == []


def test_direct_normalization_still_rejects_unrepaired_enumeration():
    with pytest.raises(BrainstormGenerationError, match="题干重复列举多个选项"):
        BrainstormCoordinator._normalize_question(enumerating_tree()["questions"][0])
