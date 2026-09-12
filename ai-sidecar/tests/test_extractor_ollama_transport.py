"""验证真实传输边界，避免仅 mock 提炼器而漏掉原生模型的协议差异。"""

import json

import httpx
import pytest
from model_schema import decoding_schema

from inference_queue import QueueEvictedError
from knowledge.extractor_v2 import (
    DATA_FACT_RECOVERY_RESPONSE_SCHEMA,
    BakeOutputTruncatedError,
    KnowledgeExtractorV2,
)


class ModelStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield json.dumps(chunk).encode("utf-8") + b"\n"

    async def aclose(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.AsyncClient

    def build(chunks, model="qwen3.5:4b", base_url="http://127.0.0.1:11434"):
        stream = ModelStream(chunks)
        calls = []

        def respond(request):
            calls.append((str(request.url), {
                "json": json.loads(request.content), "stream": True,
            }))
            return httpx.Response(200, stream=stream)

        session = real_client(transport=httpx.MockTransport(respond))
        session.calls = calls

        def make_client(**kwargs):
            session.observed_trust_env = kwargs["trust_env"]
            return session

        monkeypatch.setattr("inference_transport.httpx.AsyncClient", make_client)
        extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
        extractor.model = model
        extractor.ollama_base_url = base_url
        extractor.timeout = 180
        return extractor, session, stream

    return build


@pytest.mark.parametrize("done_reason", ["stop", "length"])
def test_qwen_raw_stream_preserves_schema_options_and_usage(transport, done_reason):
    extractor, session, response = transport([
        {"response": '{"data_'},
        {"response": 'facts":[]}'},
        {"done": True, "done_reason": done_reason, "prompt_eval_count": 123, "eval_count": 7},
    ])
    options = {"num_ctx": 32768, "num_predict": 8192}
    result = extractor._ollama_chat(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}],
        format=decoding_schema(DATA_FACT_RECOVERY_RESPONSE_SCHEMA),
        options=options,
    )
    url, request = session.calls[0]
    payload = request["json"]
    assert url.endswith("/api/generate")
    assert payload["raw"] is True
    assert "messages" not in payload and "think" not in payload
    assert payload["prompt"] == (
        "<|im_start|>system\nsystem<|im_end|>\n"
        "<|im_start|>user\nuser<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    assert payload["format"] == decoding_schema(DATA_FACT_RECOVERY_RESPONSE_SCHEMA)
    assert payload["options"] == options
    assert payload["keep_alive"] == "10m"
    assert request["stream"] and payload["stream"]
    assert not session.observed_trust_env
    assert result["message"]["content"] == '{"data_facts":[]}'
    assert result["done_reason"] == done_reason
    assert result["prompt_eval_count"] == 123 and result["eval_count"] == 7
    assert "response" not in result
    assert session.is_closed and response.closed


@pytest.mark.parametrize("base_url,trust_env", [
    ("http://localhost:11434", False),
    ("http://[::1]:11434", False),
    ("https://model.example.invalid", True),
])
def test_other_models_keep_chat_transport_and_remote_proxy(transport, base_url, trust_env):
    extractor, session, _ = transport([
        {"message": {"content": "one", "thinking": "thought"}},
        {"message": {"content": "two"}, "done": True},
    ], model="other-model", base_url=base_url)
    messages = [{"role": "user", "content": "test"}]
    result = extractor._ollama_chat(messages, format="json")
    url, request = session.calls[0]
    assert url.endswith("/api/chat")
    assert request["json"]["messages"] == messages
    assert request["json"]["think"] is False
    assert request["json"]["format"] == "json"
    assert session.observed_trust_env is trust_env
    assert result["message"] == {"content": "onetwo", "thinking": "thought"}


def test_preemption_before_http_does_not_launch_request_and_unregisters(transport, monkeypatch):
    extractor, session, response = transport([{"response": "one"}])
    callbacks = []
    unregistered = []
    def register(callback):
        callbacks.append(callback)
        return lambda: unregistered.append(True)
    def check():
        if callbacks:
            raise QueueEvictedError("preempted")
    monkeypatch.setattr("inference_queue.register_current_preempt_callback", register)
    monkeypatch.setattr("inference_queue.raise_if_preempted", check)
    with pytest.raises(QueueEvictedError):
        extractor._ollama_chat([{"role": "user", "content": "test"}])
    assert session.calls == [] and unregistered == [True]


def test_raw_repetition_retains_partial_content(transport):
    extractor, _, response = transport([{"response": "x" * 4000}])
    with pytest.raises(BakeOutputTruncatedError) as error:
        extractor._ollama_chat([{"role": "user", "content": "test"}])
    assert error.value.partial_content == "x" * 4000
    assert response.closed


def test_output_guard_closes_raw_stream_with_complete_prefix(transport):
    extractor, _, response = transport([
        {"response": '{"data_facts":[{"value":""}'},
        {"response": ',{"value":"1"}]}'},
    ])
    with pytest.raises(BakeOutputTruncatedError) as error:
        extractor._ollama_chat(
            [{"role": "user", "content": "test"}], output_guard=lambda content: True,
        )
    assert error.value.partial_content == '{"data_facts":[{"value":""}'
    assert error.value.stop_reason == "ungrounded"
    assert response.closed
