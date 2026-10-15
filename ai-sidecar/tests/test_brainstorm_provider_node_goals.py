"""Provider branches own their goals; the program owns execution ordinals."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import (
    candidate, decoder_array_budget_accepts, node, prepare, provider_candidate, setup,
)
from tests.test_brainstorm_stages import topic
from tests.test_brainstorm_stage_citations import NO_MEMORY


def grouped(count=3, counts=None, leaf=False):
    value = candidate(2 if leaf else 1)
    value["question"]["options"] = [{"label": "具体办法{}".format(index + 1),
        "description": "采用分支{}的具体组织办法。".format(index + 1), "recommended": index == 1,
        "memory_choice": "original", "next_goals": ["安排分支{}细节{}".format(index + 1, item + 1)
            for item in range((counts or [0 if leaf else 1] * count)[index])]}
        for index in range(count)]
    value.pop("next_goals")
    return value


def requested(count=5, budget=8, leaf=False):
    return {**node(2 if leaf else 1), "max_options": count, "max_next_goals": 0 if leaf else budget}


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_each_actual_nonleaf_option_must_own_a_goal_after_decoder_compaction(count):
    current = requested(count)
    value = grouped(count)
    schema = Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    assert decoder_array_budget_accepts(value, schema)
    before = copy.deepcopy(value)
    compiled = Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
    assert compiled["next_goals"] == [{"option_index": index + 1, "goal": option["next_goals"][0]}
        for index, option in enumerate(value["question"]["options"])]
    assert value == before
    value["question"]["options"][-1]["next_goals"] = []
    assert not decoder_array_budget_accepts(value, schema)
    with pytest.raises(BrainstormGenerationError, match="非叶节点.*option_index={}".format(count)):
        Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)


@pytest.mark.parametrize("counts,budget", [([1, 4], 5), ([7, 1], 8), ([1, 2, 2], 5)])
def test_program_keeps_every_asymmetric_goal_in_its_true_branch_without_equal_allocation(counts, budget):
    value = grouped(len(counts), counts)
    current = requested(5, budget)
    before = copy.deepcopy(value)
    schema = Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    assert decoder_array_budget_accepts(value, schema)
    compiled = Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
    _, branches = Coordinator._bind_node_candidate(compiled, current, topic(), "direction", Coordinator.DEFAULT_LIMITS)
    assert branches == [{"option_id": chr(ord("a") + index), "goals": option["next_goals"]}
        for index, option in enumerate(value["question"]["options"])]
    assert value == before


def test_cross_branch_total_budget_is_strict_even_when_each_nested_array_is_decoder_valid():
    value = grouped(2, [5, 4])
    current = requested(5, 8)
    before = copy.deepcopy(value)
    assert decoder_array_budget_accepts(value, Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS))
    with pytest.raises(BrainstormGenerationError, match="总数.*max_next_goals=8.*不能删分支"):
        Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
    assert value == before


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(next_goals=[]),
    lambda value: value["question"].update(parent_option_id="a"),
    lambda value: value["question"]["options"][0].update(option_index=2),
    lambda value: value["question"]["options"][0].update(option_id="b"),
    lambda value: value["question"]["options"][0].update(next_goals=[{"option_index": 2, "goal": "转到其它分支"}]),
    lambda value: value["question"]["options"][0].update(extra="fake"),
    lambda value: value["question"]["options"][0].pop("next_goals"),
    lambda value: value["question"]["options"][0].update(next_goals="不是数组"),
])
def test_provider_unknown_missing_index_or_mixed_flat_fields_never_gain_a_compatibility_fallback(mutation):
    value = grouped()
    mutation(value)
    before = copy.deepcopy(value)
    with pytest.raises(BrainstormGenerationError):
        Coordinator._compile_provider_node_candidate(value, requested(), Coordinator.DEFAULT_LIMITS)
    assert value == before


def test_old_flat_provider_shape_is_rejected_while_canonical_direct_binder_remains_compatible():
    legacy = candidate()
    with pytest.raises(BrainstormGenerationError, match="仅输出question"):
        Coordinator._compile_provider_node_candidate(legacy, node(), Coordinator.DEFAULT_LIMITS)
    _, branches = Coordinator._bind_node_candidate(legacy, node(), topic(), "detail", Coordinator.DEFAULT_LIMITS)
    assert [branch["option_id"] for branch in branches] == ["a", "b"]


@pytest.mark.parametrize("counts", [[0, 0], [0, 1]])
def test_leaf_schema_and_program_only_allow_empty_branch_goal_arrays(counts):
    value = grouped(2, counts, leaf=True)
    current = requested(2, leaf=True)
    schema = Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    assert decoder_array_budget_accepts(value, schema) == (counts == [0, 0])
    if counts == [0, 0]:
        compiled = Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
        assert compiled["next_goals"] == []
    else:
        with pytest.raises(BrainstormGenerationError, match="叶节点.*option_index=2"):
            Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)


def test_duplicate_goals_and_parent_goal_repetition_still_use_original_binder_gate():
    current = requested()
    for goals in [["重复目标", "重复目标"], [current["goal"]]]:
        value = grouped()
        value["question"]["options"][0]["next_goals"] = goals
        compiled = Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
        with pytest.raises(BrainstormGenerationError, match="重复"):
            Coordinator._bind_node_candidate(compiled, current, topic(), "direction", Coordinator.DEFAULT_LIMITS)


@pytest.mark.asyncio
async def test_actual_generation_groups_keep_original_ordinal_binding_after_recommendation_display_sort():
    value = grouped(3, [1, 2, 1])
    coordinator, service = setup([value])
    result = await prepare(coordinator, node=requested())
    assert len(service.model_calls) == 1
    assert [option["id"] for option in result["question"]["options"]] == ["b", "a", "c"]
    assert result["next_goals"] == [{"option_id": identifier, "goals": option["next_goals"]}
        for identifier, option in zip(["a", "b", "c"], value["question"]["options"])]
    assert all("next_goals" not in option for option in result["question"]["options"])
    assert "所有options内next_goals的目标总数合计不超过max_next_goals" in service.model_calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_structurally_complete_groups_still_reach_and_obey_original_semantic_gate():
    value = grouped()
    original = copy.deepcopy(value)
    coordinator, service = setup([value, value], check_consistency=True)
    inspected = []
    async def reject_policy(**kwargs):
        inspected.append(copy.deepcopy(kwargs))
        assert [branch["option_id"] for branch in kwargs["result"]["next_goals"]] == ["a", "b", "c"]
        assert len(kwargs["result"]["question"]["options"]) == 3
        raise _StageConsistencyError("topic_scope", failed_checks=[
            {"unit_id": "q0_o2", "field": "violates_selected_skill_policy"}])
    coordinator._check_stage_consistency = reject_policy
    skills = [{"id": "policy", "executionSteps": [{"instruction": "完整保留原政策"}]}]
    with pytest.raises(BrainstormGenerationError, match="当前节点未返回完整有效候选"):
        await prepare(coordinator, node=requested(), selected_skills=skills)
    assert len(inspected) == 2 and len(service.model_calls) == 2 and value == original
    assert all(item["task"]["user_constraints"]["selected_skills"] == skills for item in inspected)
    feedback = json.loads(service.prompts[1])["validation_failures"][0]
    assert feedback["code"] == "topic_scope" and feedback["failed_checks"][0]["unit_id"] == "q0_o2"


def test_fixture_adapter_is_test_only_and_never_drops_malformed_indices_or_changes_public_material():
    legacy = candidate()
    malformed = copy.deepcopy(legacy)
    malformed["next_goals"][0]["option_index"] = 99
    assert provider_candidate(malformed) == malformed
    public = {"question": {"prompt": "完整公共题干", "options": []}, "next_goals": []}
    assert provider_candidate(public) == public


def test_actual_two_goal_budget_reserves_one_complete_goal_for_the_other_option():
    current = requested(2, 2)
    schema = Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    goal_schema = schema["properties"]["question"]["properties"]["options"]["items"]["properties"]["next_goals"]
    assert goal_schema["minItems"] == goal_schema["maxItems"] == 1
    valid = grouped(2, [1, 1])
    assert decoder_array_budget_accepts(valid, schema)
    compiled = Coordinator._compile_provider_node_candidate(valid, current, Coordinator.DEFAULT_LIMITS)
    assert len(compiled["next_goals"]) == 2
    overflow = grouped(2, [2, 1])
    before = copy.deepcopy(overflow)
    assert not decoder_array_budget_accepts(overflow, schema)
    with pytest.raises(BrainstormGenerationError):
        Coordinator._compile_provider_node_candidate(overflow, current, Coordinator.DEFAULT_LIMITS)
    assert overflow == before


def test_reserve_schema_keeps_seven_plus_one_and_does_not_equal_allocate_by_max_options():
    current = requested(5, 8)
    schema = Coordinator._node_schema(current, NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    goal_schema = schema["properties"]["question"]["properties"]["options"]["items"]["properties"]["next_goals"]
    assert goal_schema["maxItems"] == 7
    value = grouped(2, [7, 1])
    before = copy.deepcopy(value)
    assert decoder_array_budget_accepts(value, schema)
    compiled = Coordinator._compile_provider_node_candidate(value, current, Coordinator.DEFAULT_LIMITS)
    assert [item["option_index"] for item in compiled["next_goals"]] == [1] * 7 + [2]
    assert value == before
