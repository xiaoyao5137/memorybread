"""脑暴结构化输出截断的阶段恢复，不能提交截断前缀或伪造历史承接。"""
import asyncio
import json

import httpx
import pytest

from creation.brainstorm import BrainstormCoordinator, BrainstormGenerationError
from creation.operations import OperationError
from tests.test_brainstorm_memory import QUOTE, reference
from tests.test_creation_brainstorm import StubCreationService, concise_question_payload


class InterruptedService(StubCreationService):
    def __init__(self, question_results, memory_results=None):
        super().__init__(question_results)
        self.memory_results = list(memory_results or [])

    def retrieve_references(self, *args, **kwargs):
        return [reference()] if self.memory_results else []

    async def _stream_direct_completion(self, **kwargs):
        self.model_calls.append(kwargs)
        self.prompts.append(kwargs["user_prompt"])
        is_memory = "历史决定核对器" in kwargs["system_prompt"]
        result = (self.memory_results if is_memory else self.responses).pop(0)
        if isinstance(result, tuple):
            yield json.dumps(result[0], ensure_ascii=False)
            raise result[1]
        if isinstance(result, Exception):
            raise result
        yield json.dumps(result, ensure_ascii=False)


def truncated(payload):
    # 即便已收到合法 JSON，最终 length 也表示候选不完整，必须整份丢弃。
    return payload, OperationError("CREATION_DOCUMENT_TRUNCATED", "private raw response")


async def next_step(service):
    return await BrainstormCoordinator(service).next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )


@pytest.mark.asyncio
async def test_truncated_question_regenerates_with_larger_budget_without_prefix(caplog):
    old = concise_question_payload("已被丢弃的问题？")
    final = concise_question_payload("下一步优先改善什么？")
    service = InterruptedService([truncated(old), final])
    result = await next_step(service)
    assert result["question"]["prompt"] == final["question"]["prompt"]
    assert len(service.model_calls) == 2
    assert service.model_calls[1]["num_predict"] > service.model_calls[0]["num_predict"]
    assert all(call["disable_thinking"] and call["json_mode"] for call in service.model_calls)
    assert "private raw response" not in caplog.text


@pytest.mark.asyncio
async def test_question_budget_exhaustion_is_bounded_and_has_brainstorm_error():
    service = InterruptedService([truncated({})] * BrainstormCoordinator.MAX_GENERATION_ATTEMPTS)
    with pytest.raises(BrainstormGenerationError) as failure:
        await next_step(service)
    assert failure.value.code == "BRAINSTORM_MODEL_OUTPUT_TRUNCATED"
    assert "长度上限" in str(failure.value)
    assert len(service.model_calls) == BrainstormCoordinator.MAX_GENERATION_ATTEMPTS
    assert max(call["num_predict"] for call in service.model_calls) <= BrainstormCoordinator.MAX_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_memory_truncation_retries_and_only_inherits_verified_complete_candidate():
    facts = {"inherited_facts": [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": QUOTE}]}
    final = concise_question_payload("主要使用者需要什么？")
    final["question"]["dimension_id"] = "users_workflow"
    service = InterruptedService([final], [truncated(facts), facts])
    result = await next_step(service)
    assert result["question"]["dimension_id"] == "users_workflow"
    assert "沿用历史结论" in result["memory_brief"]
    assert service.model_calls[1]["num_predict"] > service.model_calls[0]["num_predict"]
    schema = service.model_calls[0]["json_schema"]
    properties = schema["properties"]["inherited_facts"]["items"]["properties"]
    assert properties["evidence_id"]["enum"] == ["m1e1"]
    assert "business_outcome" in properties["dimension_id"]["enum"]
    assert schema["required"] == ["inherited_facts"]


@pytest.mark.asyncio
async def test_evidence_id_is_normalized_without_copying_quote_and_logs_only_stage_metrics(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="creation.brainstorm")
    final = concise_question_payload("主要使用者需要什么？")
    final["question"]["dimension_id"] = "users_workflow"
    service = InterruptedService([final], [{"inherited_facts": [
        {"dimension_id": "business_outcome", "evidence_id": "m1e1"},
    ]}])
    result = await next_step(service)

    assert len(service.model_calls) == 2
    assert result["question"]["dimension_id"] == "users_workflow"
    assert QUOTE in result["memory_brief"]
    assert "沿用历史结论" in result["memory_brief"]
    context = json.JSONDecoder().raw_decode(service.model_calls[0]["user_prompt"])[0]
    source = context["memory"]["sources"][0]
    assert source["evidence"] == [{"evidence_id": "m1e1", "quote": QUOTE}]
    assert source["updated_at"] == 1788566400000
    assert "current_user_intent" in context
    stage_logs = [record.getMessage() for record in caplog.records
                  if record.getMessage().startswith("Brainstorm stage=")]
    assert any("stage=memory_query " in value for value in stage_logs)
    assert any("stage=memory_retrieval " in value for value in stage_logs)
    assert any("stage=memory_assessment " in value for value in stage_logs)
    assert any("stage=question_generation " in value for value in stage_logs)
    assert all("duration_ms=" in value and "count=" in value and "error_type=" in value
               for value in stage_logs)
    assert all(QUOTE not in value and "原创剧本" not in value for value in stage_logs)


@pytest.mark.asyncio
async def test_truncated_evidence_id_candidate_cannot_inherit_a_decision():
    facts = {"inherited_facts": [{"dimension_id": "business_outcome", "evidence_id": "m1e1"}]}
    service = InterruptedService([concise_question_payload("下一步优先改善什么？")],
                                 [truncated(facts), {"inherited_facts": []}])
    result = await next_step(service)
    assert "沿用历史结论" not in result["memory_brief"]
    assert result["question"]["dimension_id"] == "business_outcome"
    assert len(service.model_calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    truncated({"inherited_facts": [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": QUOTE}]}),
    httpx.ReadTimeout("private endpoint"),
    {"inherited_facts": [{"dimension_id": "business_outcome", "memory_id": "m1", "quote": "不存在的伪造历史决定。"}]},
])
async def test_unreliable_optional_memory_keeps_coverage_pending_and_continues(failure, caplog):
    service = InterruptedService([concise_question_payload("下一步优先改善什么？")], [failure, failure])
    result = await next_step(service)
    assert result["question"]["dimension_id"] == "business_outcome"
    assert "目标与期望结果" in result["open_flags"]
    assert "沿用历史结论" not in result["memory_brief"]
    assert "历史决定未能可靠核对" in result["memory_brief"]
    assert QUOTE in service.model_calls[-1]["user_prompt"]
    assert len(service.model_calls) == 3
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["memory", "question"])
async def test_non_truncation_operation_error_is_never_hidden(stage):
    failure = OperationError("MODEL_ACCESS_DENIED", "模型访问未获授权")
    service = InterruptedService([failure], [failure] if stage == "memory" else None)
    with pytest.raises(OperationError) as result:
        await next_step(service)
    assert result.value is failure
    assert len(service.model_calls) == 1


@pytest.mark.asyncio
async def test_memory_deadline_cancels_slow_stream_and_still_generates_question(monkeypatch):
    cancelled = []

    class SlowMemoryService(InterruptedService):
        async def _stream_direct_completion(self, **kwargs):
            if "历史决定核对器" in kwargs["system_prompt"]:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            async for chunk in super()._stream_direct_completion(**kwargs):
                yield chunk

    monkeypatch.setattr(BrainstormCoordinator, "MAX_MEMORY_ASSESSMENT_SECONDS", 0.02)
    service = SlowMemoryService([concise_question_payload("下一步优先改善什么？")], [{}])
    result = await next_step(service)
    assert cancelled == [True]
    assert result["question"]["dimension_id"] == "business_outcome"
    assert "历史决定未能可靠核对" in result["memory_brief"]


@pytest.mark.asyncio
async def test_turn_deadline_cancels_generation_instead_of_orphaned_retry(monkeypatch):
    cancelled = []

    class SlowQuestionService(InterruptedService):
        async def _stream_direct_completion(self, **kwargs):
            try:
                await asyncio.Event().wait()
                yield ""
            finally:
                cancelled.append(True)

    monkeypatch.setattr(BrainstormCoordinator, "MAX_TURN_SECONDS", 0.02)
    with pytest.raises(BrainstormGenerationError) as failure:
        await next_step(SlowQuestionService([]))
    assert failure.value.code == "BRAINSTORM_MODEL_TIMEOUT"
    assert cancelled == [True]
