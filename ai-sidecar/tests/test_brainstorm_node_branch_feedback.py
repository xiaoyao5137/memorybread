"""Branch repair must describe the actual node, without opposite leaf tasks."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_brainstorm_stage_nodes import candidate, node, prepare, setup
from tests.test_brainstorm_stages import topic


def five_options(value):
    value = copy.deepcopy(value)
    for index in range(3):
        option = copy.deepcopy(value["question"]["options"][0])
        option.update(label="组织方法{}".format(index + 3), description="通过尚未决定的方法{}组织表达".format(index + 3))
        value["question"]["options"].append(option)
    return value


def full_goals():
    return [{"option_index": index, "goal": "安排分支{}的具体说明顺序".format(index)} for index in range(1, 6)]


def error_message(value, requested):
    with pytest.raises(BrainstormGenerationError) as error:
        BrainstormStageCoordinator._bind_node_candidate(value, requested, topic(), "direction",
            BrainstormStageCoordinator.DEFAULT_LIMITS)
    return str(error.value)


def test_nonleaf_five_options_two_goals_report_real_coverage_and_never_request_leaf_behavior():
    value = five_options(candidate())
    value["next_goals"] = full_goals()[:2]
    message = error_message(value, {**node(), "max_options": 5, "max_next_goals": 8})
    assert "当前节点为非叶节点" in message and "叶节点不得继续展开" not in message
    assert "depth=1" in message and "max_depth=2" in message
    assert "optionsCount=5" in message and "nextGoalCount=2" in message
    assert "option_index=[1, 2, 3, 4, 5]" in message
    assert "不得删除选项" in message and "max_next_goals=8" in message
    assert not any(option["description"] in message for option in value["question"]["options"])


def test_leaf_goals_only_request_empty_future_not_nonleaf_expansion():
    value = candidate(2)
    value["next_goals"] = full_goals()[:2]
    message = error_message(value, node(2))
    assert "当前节点为叶节点" in message and "非叶节点" not in message
    assert "depth=2" in message and "max_depth=2" in message and "nextGoalCount=2" in message
    assert "next_goals必须为空" in message and "完整覆盖" not in message


def test_sufficient_goal_count_still_identifies_the_actual_uncovered_option_index():
    value = five_options(candidate())
    value["next_goals"] = full_goals()
    value["next_goals"][2]["option_index"] = 1
    message = error_message(value, {**node(), "max_options": 5, "max_next_goals": 8})
    assert "缺少option_index=3的后续目标" in message
    assert "nextGoalCount=5" in message and "叶节点不得继续展开" not in message


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.asyncio
async def test_actual_repair_feedback_only_instructs_the_matching_leaf_or_nonleaf_contract(leaf):
    requested = node(2) if leaf else {**node(), "max_options": 5, "max_next_goals": 8}
    good = candidate(2) if leaf else five_options(candidate())
    good["next_goals"] = [] if leaf else full_goals()
    bad = copy.deepcopy(good)
    bad["next_goals"] = full_goals()[:2]
    coordinator, service = setup([bad, good])
    result = await prepare(coordinator, node=requested)
    assert len(service.model_calls) == 2
    feedback = json.loads(service.prompts[1])["validation_failures"]
    assert len(feedback) == 1 and isinstance(feedback[0], str)
    if leaf:
        assert "当前节点为叶节点" in feedback[0] and "非叶节点" not in feedback[0]
        assert result["next_goals"] == []
    else:
        assert "当前节点为非叶节点" in feedback[0] and "叶节点不得继续展开" not in feedback[0]
        assert "optionsCount=5" in feedback[0] and "nextGoalCount=2" in feedback[0]
        assert len(result["question"]["options"]) == 5 and len(result["next_goals"]) == 5
