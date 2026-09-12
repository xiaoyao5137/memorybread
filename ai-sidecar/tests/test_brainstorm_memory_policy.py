"""模型可按选项选择原创或历史依据，同时保留权限和证据边界。"""

import copy

import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from tests.test_brainstorm_memory import MemoryService, QUOTE, payload
from tests.test_creation_brainstorm import concise_question_payload


def original_payload():
    value = concise_question_payload("剧本可以尝试什么新的呈现方式？")
    value["question"]["exploration_stage"] = "solutions"
    value["question"]["options"][0].update(
        label="倒序揭示冲突", description="从结果切入再逐步揭开原因，增强悬念。",
    )
    value["question"]["options"][1].update(
        label="交替讲述人物视角", description="让不同角色补齐同一事件，呈现立场差异。",
    )
    for option in value["question"]["options"]:
        option.update(memory_mode="original", memory_ids=[])
    return value


@pytest.mark.asyncio
async def test_all_original_options_are_valid_despite_memory_hits_without_extra_model_round():
    service = MemoryService([original_payload()])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown="",
        exploration_stage="solutions", focus_hint="尝试新的呈现方式",
    )
    assert service.queries and len(service.model_calls) == 1
    assert "整题所有选项都可以是原创思路" in service.model_calls[0]["system_prompt"]
    assert "尽量用相关资料支持推荐" not in service.model_calls[0]["system_prompt"]
    assert "本轮已检索历史记忆" in result["question"]["context_details"]
    assert "记忆依据：" not in result["memory_brief"]
    for option in result["question"]["options"]:
        assert "未附历史记忆引用" in option["details"]
        assert "通用知识" not in option["details"]
        assert "待验证推演" not in option["details"]
    assert result["question"]["options"][0]["recommended"] is True


@pytest.mark.asyncio
async def test_original_recommendation_can_coexist_with_verified_reference_alternative():
    value = payload()
    referenced, original = value["question"]["options"]
    referenced.update(memory_mode="reference", recommended=False)
    original.update(memory_mode="original", memory_ids=[], recommended=True)
    service = MemoryService([value])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown="",
    )
    first, second = result["question"]["options"]
    assert first["label"] == original["label"] and "未附历史记忆引用" in first["details"]
    assert second["label"] == referenced["label"] and QUOTE in second["details"]
    assert "document:1" in second["details"] and QUOTE in result["memory_brief"]
    assert QUOTE not in first["details"]


@pytest.mark.parametrize("change", [
    {"memory_mode": "reference", "memory_ids": [], "memory_evidence": []},
    {"memory_mode": "original", "memory_ids": ["m1"], "memory_evidence": []},
    {"memory_mode": "original", "memory_evidence": [{"memory_id": "m1", "quote": QUOTE}]},
    {"memory_mode": "original", "label": "记忆表明需要改进", "memory_evidence": []},
    {"memory_mode": "unsupported", "memory_evidence": []},
    {"memory_mode": None, "memory_evidence": []},
])
def test_contradictory_modes_cannot_bypass_evidence_validation(change):
    value = payload()
    value["question"]["options"][0].update(change)
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})


@pytest.mark.parametrize("source_or_quote", [
    {"memory_id": "m99", "quote": QUOTE},
    {"memory_id": "m1", "quote": "过去已经决定自动发布所有剧本，无需人工审核。"},
])
def test_reference_mode_still_rejects_fabricated_sources_and_quotes(source_or_quote):
    value = payload()
    value["question"]["options"][0].update(
        memory_mode="reference", memory_evidence=[source_or_quote],
    )
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})


@pytest.mark.parametrize("mode", [None, "reference"])
def test_valid_quote_cannot_hide_an_unknown_explicit_memory_id(mode):
    value = payload()
    value["question"]["options"][0]["memory_ids"] = ["m99"]
    if mode is not None:
        value["question"]["options"][0]["memory_mode"] = mode
    with pytest.raises(BrainstormGenerationError, match="不存在的记忆来源"):
        BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})


@pytest.mark.asyncio
async def test_reference_without_evidence_repairs_to_original_without_retrieving_again():
    invalid = original_payload()
    invalid["question"]["options"][0]["memory_mode"] = "reference"
    service = MemoryService([invalid, original_payload()])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown="",
    )
    assert len(service.model_calls) == 2 and len(service.queries) == 1
    assert all("未附历史记忆引用" in option["details"] for option in result["question"]["options"])


@pytest.mark.asyncio
async def test_original_mode_preserves_explicit_no_memory_access_before_retrieval():
    class NoMemoryAccessService(MemoryService):
        def analyze_requirement(self, *args, **kwargs):
            raise AssertionError("明确禁用记忆时不得分析检索查询")

        def retrieve_references(self, *args, **kwargs):
            raise AssertionError("明确禁用记忆时不得访问资料")

    service = NoMemoryAccessService([original_payload()])
    result = await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案，只用当前输入", decisions=[], brief_markdown="",
    )
    assert "未检索历史记忆" in result["question"]["context_details"]
    assert not service.queries and len(service.model_calls) == 1
    assert "记忆依据：" not in result["memory_brief"]


def test_omitted_mode_preserves_legacy_original_and_source_id_candidates():
    value = concise_question_payload("应该优先改善哪个环节？")
    value["question"]["options"][0]["memory_ids"] = ["m1"]
    BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})
    assert value["question"]["options"][0]["memory_evidence"] == [{"memory_id": "m1", "quote": QUOTE}]
    assert not value["question"]["options"][1].get("memory_evidence")


def test_memory_modes_also_apply_to_continuation_directions():
    original = original_payload()["question"]["options"]
    value = {"status": "ready", "continuation_directions": copy.deepcopy(original)}
    BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})
    value["continuation_directions"][0]["memory_mode"] = "reference"
    with pytest.raises(BrainstormGenerationError):
        BrainstormCoordinator._ground_options(value, {"sources": [{"memory_id": "m1", "content": QUOTE}]})


def test_generation_schema_requires_adoption_mode_while_parser_preserves_legacy_omission():
    schema = BrainstormCoordinator._generation_schema(
        suggest_directions=False, require_question=True, dimension_id="", exploration_stage="",
        memory_ids=["m1"],
    )
    option = schema["properties"]["question"]["properties"]["options"]["items"]
    assert option["properties"]["memory_mode"]["enum"] == ["original", "reference"]
    assert "memory_mode" in option["required"]
