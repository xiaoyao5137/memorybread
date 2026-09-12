"""作答文案预算、证据分层与有界重写回归。"""
import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from tests.test_brainstorm_memory import MemoryService, QUOTE, payload, reference


@pytest.mark.asyncio
@pytest.mark.parametrize("field,limit", [("prompt", 60), ("why_now", 80), ("label", 24), ("description", 80)])
async def test_overlong_copy_is_rewritten_without_truncating_choices_or_retrieving_again(field, limit):
    invalid = payload()
    target = invalid["question"] if field in ("prompt", "why_now") else invalid["question"]["options"][0]
    target[field] = "长" * limit + "必须保留的条件"
    valid = payload()
    service = MemoryService([invalid, valid])
    result = await BrainstormCoordinator(service).next_step(root_request="设计剧本创作方案", decisions=[], brief_markdown="")
    assert len(service.prompts) == 2
    assert len(service.queries) == 1
    assert "超过 {} 字符".format(limit) in service.prompts[1]
    assert result["question"]["prompt"] == valid["question"]["prompt"]
    assert [item["label"] for item in result["question"]["options"]] == [item["label"] for item in valid["question"]["options"]]
    assert QUOTE in result["question"]["options"][0]["details"]


@pytest.mark.parametrize("field,limit", [("label", 24), ("description", 80)])
def test_continuation_copy_obeys_same_budget(field, limit):
    directions = payload()["question"]["options"]
    directions[0][field] = "字" * (limit + 1)
    with pytest.raises(BrainstormGenerationError, match="超过 {} 字符".format(limit)):
        BrainstormCoordinator._normalize_directions(directions)


@pytest.mark.asyncio
async def test_long_source_titles_and_inherited_quotes_stay_in_details_and_brief():
    value = payload()
    value["question"]["dimension_id"] = "users_workflow"
    value["question"]["options"][0]["description"] = "依据 m1 的目标完善评审反馈，减少反复改稿。"
    value["inherited_facts"] = [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": QUOTE}]
    source = reference()
    source.title = "长期历史决策原文标题" * 16
    service = MemoryService([value], [source])
    result = await BrainstormCoordinator(service).next_step(root_request="设计剧本创作方案", decisions=[], brief_markdown="")
    question = result["question"]
    assert len(service.prompts) == 1
    assert len(question["why_now"]) <= 80
    assert "沿用历史结论" not in question["why_now"]
    assert QUOTE in question["context_details"] and QUOTE in result["memory_brief"]
    option = question["options"][0]
    assert len(option["description"]) <= 80
    assert "长期历史决策" not in option["description"]
    assert source.title[:160] in option["details"] and QUOTE in option["details"]
    assert option["description"] == "依据 相关资料 的目标完善评审反馈，减少反复改稿。"


def test_final_copy_guard_catches_expansion_after_normalization():
    question = BrainstormCoordinator._normalize_question(payload()["question"])
    question["options"][0]["description"] = "后处理膨胀" * 30
    with pytest.raises(BrainstormGenerationError, match="options.description"):
        BrainstormCoordinator._validate_display_copy({"question": question})


@pytest.mark.asyncio
async def test_readability_retries_are_bounded_and_do_not_publish_cut_off_text():
    invalid = payload()
    invalid["question"]["prompt"] = "背景" * 40 + "关键问题是什么？"
    service = MemoryService([invalid] * BrainstormCoordinator.MAX_GENERATION_ATTEMPTS)
    with pytest.raises(BrainstormGenerationError):
        await BrainstormCoordinator(service).next_step(root_request="设计剧本创作方案", decisions=[], brief_markdown="")
    assert len(service.prompts) == BrainstormCoordinator.MAX_GENERATION_ATTEMPTS
    assert len(service.queries) == 1
