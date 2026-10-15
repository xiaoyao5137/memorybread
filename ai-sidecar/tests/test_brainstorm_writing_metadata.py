"""Program-owned writing metadata cannot leak model field declarations."""
import copy
import json

import pytest

from creation.brainstorm import BrainstormGenerationError
from creation.brainstorm_stages import BrainstormStageCoordinator as Coordinator, _StageConsistencyError
from tests.test_brainstorm_stage_nodes import candidate, fixed_decision, node, prepare, setup
from tests.test_brainstorm_writing_scope import writing_candidate


FIELDS = {"type", "prompt_body", "why_now", "options"}
NO_MEMORY = {"status": "skipped", "sources": []}


def writing_node(depth, maximum=3):
    requested = node(depth, maximum)
    requested["goal"] = Coordinator.WRITING_AXES[0]["levels"][depth - 1]
    return requested


def writing_topic():
    return {"id": "topic", "title": "当前章节", "goal": Coordinator.WRITING_AXES[0]["goal"],
        "chapter_id": "chapter1"}


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_program_writing_decoder_generates_only_four_natural_fields(depth):
    requested = writing_node(depth)
    schema = Coordinator._node_schema(requested, NO_MEMORY,
        {**Coordinator.DEFAULT_LIMITS, "max_depth": 3}, writing_axis=Coordinator.WRITING_AXES[0])
    question = schema["properties"]["question"]
    assert set(question["properties"]) == set(question["required"]) == FIELDS
    assert question["additionalProperties"] is False
    assert question["properties"]["type"]["enum"] == ["single_choice", "multi_choice"]
    assert (question["properties"]["options"]["minItems"],
        question["properties"]["options"]["maxItems"]) == (2, requested["max_options"])


@pytest.mark.parametrize("question_type", ["single_choice", "multi_choice"])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_metadata_binding_preserves_actual_type_options_and_complete_program_paths(question_type, depth):
    requested = writing_node(depth)
    value = {"question": writing_candidate(depth, 3)["question"]}
    value["question"]["type"] = question_type
    value["question"]["options"][0]["recommended"] = False
    value["question"]["options"][1]["recommended"] = True
    original = copy.deepcopy(value)
    result, future = Coordinator._bind_node_candidate(value, requested, writing_topic(), "writing",
        {**Coordinator.DEFAULT_LIMITS, "max_depth": 3}, writing_axis=Coordinator.WRITING_AXES[0])
    actual = result["questions"][0]
    assert actual["type"] == question_type and value == original
    assert [{key: option[key] for key in original["question"]["options"][index]}
        for index, option in enumerate(actual["options"])] == original["question"]["options"]
    assert actual["answer_template"] == "可补充你的具体组织方式。"
    assert actual["single_choice_reason"] == ("同一组织目标需要选择一种实施方式。"
        if question_type == "single_choice" else "")
    assert all(len(actual[field]) <= 80 for field in ("single_choice_reason", "answer_template"))
    assert future == ([] if depth == 3 else [{"option_id": identifier,
        "goals": [Coordinator.WRITING_AXES[0]["levels"][depth]]} for identifier in ("a", "b")])


@pytest.mark.parametrize("field,value", [
    ("answer_template", "label: string, description: string, recommended: boolean, memory_choice: 'original' | 'reference'"),
    ("single_choice_reason", "模型添加的单选元数据"),
    ("id", "model-overwrites-node"),
])
def test_program_writing_metadata_is_strictly_owned_by_program(field, value):
    raw = {"question": writing_candidate()["question"]}
    raw["question"][field] = value
    with pytest.raises(BrainstormGenerationError, match="执行元数据"):
        Coordinator._bind_node_candidate(raw, writing_node(1), writing_topic(), "writing",
            Coordinator.DEFAULT_LIMITS, writing_axis=Coordinator.WRITING_AXES[0])


@pytest.mark.parametrize("question_type", ["single_choice", "multi_choice"])
@pytest.mark.asyncio
async def test_complete_writing_turn_binds_natural_metadata_without_extra_generation(question_type):
    raw = {"question": writing_candidate()["question"]}
    raw["question"]["type"] = question_type
    coordinator, service = setup([raw])
    seen = []
    async def inspect(**kwargs):
        seen.append(copy.deepcopy(kwargs))
    coordinator._check_stage_consistency = inspect
    result = await prepare(coordinator, stage="writing")
    assert result["question"]["type"] == "single_choice"
    assert result["question"]["answer_template"] == "可补充你的具体组织方式。"
    assert result["question"]["single_choice_reason"] == "同一组织目标需要选择一种实施方式。"
    assert service.model_calls == [] and len(service.responses) == 1
    assert len(seen) == 1 and seen[0]["result"] == result
    packet = seen[0]["task"]["editorial_compilation"]["packet"]
    assert set(packet["parsed"]["question"]) == FIELDS
    assert packet["parsed"]["question"]["type"] == "single_choice"
    assert packet["next_goals"] == result["next_goals"]
    Coordinator._verify_editorial_compilation(result=result, task=seen[0]["task"], topic=seen[0]["topic"])


@pytest.mark.parametrize("stage", ["direction", "detail"])
def test_other_stages_keep_original_model_owned_metadata_and_type_contract(stage):
    value = candidate()
    value["question"]["type"] = "single_choice"
    value["question"]["single_choice_reason"] = "两个方法在实际安排中互斥"
    value["question"]["answer_template"] = "补充你的现实限制"
    schema = Coordinator._node_schema(node(), NO_MEMORY, Coordinator.DEFAULT_LIMITS)
    assert {"single_choice_reason", "answer_template"}.issubset(schema["properties"]["question"]["properties"])
    actual, future = Coordinator._bind_node_candidate(value, node(),
        {"id": "topic", "title": "普通主题", "goal": "比较具体表达方式"}, stage, Coordinator.DEFAULT_LIMITS)
    assert actual["questions"][0]["single_choice_reason"] == value["question"]["single_choice_reason"]
    assert actual["questions"][0]["answer_template"] == value["question"]["answer_template"]
    assert future == [{"option_id": "a", "goals": ["比較人物视角", "安排人物出场"]},
        {"option_id": "b", "goals": ["比较场景节奏"]}]


@pytest.mark.asyncio
async def test_bound_metadata_does_not_publish_a_reopened_confirmed_organization_method():
    raw = {"question": writing_candidate()["question"]}
    raw["question"].update(type="single_choice", prompt_body="逐行说明的原行定位应放在哪里？")
    raw["question"]["options"][0].update(label="定位放开头", description="先写项目与原行定位，再解释原指标")
    raw["question"]["options"][1].update(label="定位放结尾", description="先解释原指标，再写原行定位")
    coordinator, service = setup([raw, raw], check_consistency=True)
    seen = []
    async def reject_reopened(**kwargs):
        seen.append(copy.deepcopy(kwargs))
        raise _StageConsistencyError("confirmed_decision_reopened", ["decision_1"])
    coordinator._check_stage_consistency = reject_reopened
    original = fixed_decision(question="逐行说明的原行定位放在哪里？", answer="定位放开头")
    before = copy.deepcopy(original)
    with pytest.raises(_StageConsistencyError) as error:
        await prepare(coordinator, stage="writing", decisions=[original])
    assert error.value.code == "confirmed_decision_reopened" and error.value.decision_ids == ["decision_1"]
    assert original == before and service.model_calls == [] and len(service.responses) == 2
    assert len(seen) == 1
    assert seen[0]["result"]["question"]["answer_template"] == "可补充你的具体组织方式。"
    task = seen[0]["task"]
    assert task["program_bound_editorial"] is True
    assert task["fixed_decisions"][0]["answer"] == "定位放开头"
    assert task["editorial_compilation"]["inputs"]["confirmed_writing_choices"] == task["fixed_decisions"]
