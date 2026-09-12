"""Intent provenance tolerates typography, never rewritten requirements."""
import json

import pytest

from creation import skill_governance as governance
from creation import source_spans


def output(quote):
    return {"requests": [{"request": quote, "role": "task", "action": "create"}]}


class Model:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = []

    async def _stream_direct_completion(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(output(self.quotes[min(len(self.calls) - 1, len(self.quotes) - 1)]), ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction,quote", [
    ("创作一篇吸引非L0商家的方案，提高GMV并使用SOTA模型。", "创作一篇吸引非 L0 商家的方案，提高 GMV 并使用 SOTA 模型。"),
    ("写一份AI模型说明", "写一份 AI 模型说明"),
    ("写一份 AI 模型说明", "写一份AI模型说明"),
    ("写一份\u00a0AI\u3000模型说明", "写一份 AI 模型说明"),
    ("写一份AI模型说明", "写一份\tAI\t模型说明"),
])
async def test_typographic_spacing_is_rebound_to_unchanged_source(instruction, quote):
    model = Model([quote])
    assert await governance.task_intent(model, instruction, False) == {
        "action": "create", "primary_goal": instruction,
        "deliverable": instruction, "content_request": instruction,
    }
    assert len(model.calls) == 1
    assert json.loads(model.calls[0]["user_prompt"])["instruction"] == instruction


@pytest.mark.parametrize("instruction,quote", [
    ("请写AI模型说明；仅使用材料。", "写 AI 模型说明"),
    ("写 AI 模型说明", "写AI模型说明"),
])
def test_contract_accepts_only_a_resolvable_source_span(instruction, quote):
    result = output(quote)
    assert governance.intent_contract_problem(result, governance.intent_request_schema(), instruction) == ""
    intent = governance.aggregate_intent_requests(result, instruction)
    assert intent["primary_goal"] in instruction
    assert result == output(quote)  # Validation and aggregation must not rewrite model records.


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction,quote", [
    ("写一份面向L0商家的说明", "写一份面向 LO 商家的说明"),
    ("请不要写AI方案", "请要写 AI 方案"),
    ("写一份A B模型说明", "写一份AB模型说明"),
    ("写一份1000元说明", "写一份1 000元说明"),
    ("写一份notable案例说明", "写一份not able案例说明"),
    ("请写AI模型说明", "请写AI模型总结"),
    ("写一份AI模型说明", "写一份ai模型说明"),
    ("写一份AI模型说明", "写一份\nAI\n模型说明"),
    ("写一份AI模型说明", "写一份 AI 模型说明。"),
    ("写一份AI模型说明", "写 一份 AI 模型说明"),
])
async def test_non_typographic_changes_remain_fail_closed(instruction, quote):
    model = Model([quote])
    assert await governance.task_intent(model, instruction, False) == {}
    assert len(model.calls) == governance.INTENT_ATTEMPTS


@pytest.mark.asyncio
async def test_normalized_ambiguity_is_rejected_but_exact_match_wins():
    instruction = "写AI模型说明；写 AI模型说明"
    model = Model(["写 AI 模型说明", "写 AI模型说明"])
    intent = await governance.task_intent(model, instruction, False)
    assert intent["primary_goal"] == "写 AI模型说明"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_persistent_normalized_ambiguity_never_selects_first_occurrence():
    model = Model(["写 AI 模型说明"])
    assert await governance.task_intent(model, "写AI模型说明；写 AI模型说明", False) == {}
    assert len(model.calls) == governance.INTENT_ATTEMPTS


def test_preserved_long_whitespace_run_is_processed_in_linear_work(monkeypatch):
    instruction = "请写AI模型" + " " * 1000 + "说明"
    original_category = source_spans.unicodedata.category
    calls = 0

    def category(char):
        nonlocal calls
        calls += 1
        return original_category(char)

    monkeypatch.setattr(source_spans.unicodedata, "category", category)
    key, offsets = source_spans.typography_key(instruction)
    assert key == instruction
    assert offsets == list(range(len(instruction)))
    assert calls < len(instruction) * 3
