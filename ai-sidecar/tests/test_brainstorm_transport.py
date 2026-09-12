"""Use the production Ollama transport to verify brainstorm recovery contracts."""

import json

import httpx
import pytest

from creation.brainstorm import BrainstormCoordinator
from creation.service import CreationService
from tests.test_brainstorm_memory import QUOTE
from tests.test_creation_brainstorm import concise_question_payload


@pytest.mark.asyncio
async def test_qwen_transport_preserves_schema_and_discards_length_terminated_candidates(monkeypatch):
    service = object.__new__(CreationService)
    service.model = "qwen3.5:4b"
    service.ollama_base_url = "http://127.0.0.1:11434"
    coordinator = BrainstormCoordinator(service)
    memory = {
        "status": "matched",
        "as_of": "2026-09-08T00:00:00+00:00",
        "sources": [{
            "memory_id": "m1", "source_type": "document", "source_id": 1,
            "title": "原创剧本项目决策", "updated_at": 1788566400000,
            "observed_at": None, "content": QUOTE,
        }],
    }
    monkeypatch.setattr(coordinator, "_retrieve_memory", lambda *args: memory)
    discarded_facts = {"inherited_facts": [{
        "dimension_id": "business_outcome", "evidence_id": "m1e1",
    }]}
    final_question = concise_question_payload("下一步优先改善什么？")
    responses = [
        (discarded_facts, "length"),
        ({"inherited_facts": []}, "stop"),
        (concise_question_payload("已经丢弃的问题？"), "length"),
        (final_question, "stop"),
    ]
    requests = []

    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/api/generate"
        requests.append(json.loads(request.content))
        result, reason = responses[len(requests) - 1]
        text = json.dumps(result, ensure_ascii=False)
        # The JSON itself is complete before a separate terminal event reports
        # length. Recovery must honor the terminal event, not accept this prefix.
        events = [
            {"response": text[:len(text) // 2], "done": False},
            {"response": text[len(text) // 2:], "done": False},
            {"response": "", "done": True, "done_reason": reason},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(item) for item in events),
                              headers={"content-type": "application/x-ndjson"})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr("creation.service.httpx.AsyncClient",
                        lambda **kwargs: client_type(transport=transport, **kwargs))
    result = await coordinator.next_step(
        root_request="设计原创剧本生成方案", decisions=[], brief_markdown=""
    )

    assert len(requests) == 4
    assert result["question"]["prompt"] == final_question["question"]["prompt"]
    assert result["question"]["dimension_id"] == "business_outcome"
    assert "目标与期望结果" in result["open_flags"]
    assert "沿用历史结论" not in result["memory_brief"]
    assert requests[0]["format"] == requests[1]["format"]
    schema = requests[0]["format"]
    assert schema["required"] == ["inherited_facts"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["inherited_facts"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["evidence_id"]["enum"] == ["m1e1"]
    assert item["required"] == ["dimension_id", "evidence_id"]
    assert "business_outcome" in item["properties"]["dimension_id"]["enum"]
    assert requests[2]["format"] == requests[3]["format"]
    question_schema = requests[2]["format"]
    assert question_schema["properties"]["status"]["enum"] == ["question"]
    fields = question_schema["properties"]["question"]["properties"]
    assert fields["dimension_id"]["enum"] == ["business_outcome"]
    assert fields["type"]["enum"] == ["multi_choice", "single_choice"]
    assert requests[1]["options"]["num_predict"] > requests[0]["options"]["num_predict"]
    assert requests[3]["options"]["num_predict"] > requests[2]["options"]["num_predict"]
    for payload in requests:
        assert payload["raw"] is True and payload["stream"] is True
        assert payload["prompt"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
        assert payload["options"]["num_predict"] <= coordinator.MAX_OUTPUT_TOKENS
        assert payload["options"]["repeat_penalty"] == 1.0
        assert payload["options"]["presence_penalty"] == 0.0
