from __future__ import annotations

import asyncio

import httpx
import pytest

from creation.service import CloudModelRequestError, CreationService


async def _collect_stream(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def test_anthropic_messages_url_accepts_common_base_url_shapes():
    assert CreationService._anthropic_messages_url("") == "https://api.anthropic.com/v1/messages"
    assert CreationService._anthropic_messages_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert CreationService._anthropic_messages_url("https://api.anthropic.com/v1") == "https://api.anthropic.com/v1/messages"
    assert CreationService._anthropic_messages_url("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com/v1/messages"


def test_normalize_anthropic_messages_removes_empty_messages_and_merges_roles():
    system, messages = CreationService._normalize_anthropic_messages(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "继续"},
        ]
    )

    assert system == "Be concise."
    assert messages == [{"role": "user", "content": "在吗\n\n继续"}]


def test_raise_for_cloud_error_exposes_provider_message():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"type": "invalid_request_error", "message": "messages: text content blocks must be non-empty"}},
    )

    with pytest.raises(RuntimeError, match="text content blocks must be non-empty"):
        asyncio.run(CreationService._raise_for_cloud_error(response))


def test_generate_cloud_retries_transient_connect_error_before_first_chunk(monkeypatch):
    service = object.__new__(CreationService)
    attempts = []
    sleeps = []

    async def flaky_generate_once(*_args):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise httpx.ConnectError("temporary connection failure")
        yield "recovered"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(service, "_generate_cloud_once", flaky_generate_once)
    monkeypatch.setattr("creation.service.asyncio.sleep", fake_sleep)

    chunks = asyncio.run(
        _collect_stream(service._generate_cloud("system", "user", "model", "key", ""))
    )

    assert chunks == ["recovered"]
    assert attempts == [1, 2, 3]
    assert sleeps == [0.75, 1.5]


def test_generate_cloud_does_not_retry_after_content_was_emitted(monkeypatch):
    service = object.__new__(CreationService)
    attempts = []

    async def interrupted_generate_once(*_args):
        attempts.append(len(attempts) + 1)
        yield "partial"
        raise httpx.ReadError("stream interrupted")

    monkeypatch.setattr(service, "_generate_cloud_once", interrupted_generate_once)

    async def consume():
        chunks = []
        with pytest.raises(httpx.ReadError, match="stream interrupted"):
            async for chunk in service._generate_cloud("system", "user", "model", "key", ""):
                chunks.append(chunk)
        return chunks

    assert asyncio.run(consume()) == ["partial"]
    assert attempts == [1]


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_retryable_cloud_http_statuses(status_code):
    assert CreationService._is_retryable_cloud_error(
        CloudModelRequestError(status_code, "temporary")
    )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_non_retryable_cloud_http_statuses(status_code):
    assert not CreationService._is_retryable_cloud_error(
        CloudModelRequestError(status_code, "permanent")
    )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_specialist_does_not_retry_permanent_error_with_empty_output(monkeypatch, status, streaming):
    service = object.__new__(CreationService)
    service.model = "test-local"
    calls = []

    async def rejected(**kwargs):
        calls.append(kwargs)
        raise CloudModelRequestError(status, "private provider detail")
        yield ""

    monkeypatch.setattr(service, "_stream_direct_completion", rejected)
    monkeypatch.setattr(service, "_log_creation_usage", lambda **kwargs: None)
    with pytest.raises(CloudModelRequestError):
        kwargs = dict(agent_id="test", system_prompt="test", user_prompt="test")
        if streaming:
            asyncio.run(_collect_stream(service.stream_specialist_agent(**kwargs)))
        else:
            asyncio.run(service.run_specialist_agent(**kwargs))
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_routing_does_not_fallback_on_access_denied(monkeypatch, status):
    service = object.__new__(CreationService)
    service.model = "test-local"

    async def rejected(**kwargs):
        raise CloudModelRequestError(status, "private provider detail")
        yield ""

    monkeypatch.setattr(service, "_stream_direct_completion", rejected)
    with pytest.raises(CloudModelRequestError):
        asyncio.run(service.route_capabilities(query="test", requirement={}))


def test_qwen_non_thinking_prompt_closes_reasoning_before_generation():
    service = object.__new__(CreationService)
    prompt = service._build_qwen35_prompt("system", "user", disable_thinking=True)
    assert prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert "/no_think" not in prompt
    assert service._build_qwen35_prompt("system", "user").endswith("<|im_start|>assistant\n")

@pytest.mark.parametrize("entry", ["analysis", "skill", "document", "json"])
def test_agent_length_recovery_discards_partial_candidate(monkeypatch, entry):
    from creation.operations import OperationError
    service = object.__new__(CreationService)
    service.model = "local"
    calls = []
    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            yield "discard this truncated candidate"
            raise OperationError("CREATION_DOCUMENT_TRUNCATED", "length")
        yield '{"complete":true}' if entry == "json" else "Complete document."
    monkeypatch.setattr(service, "_stream_direct_completion", generate)
    monkeypatch.setattr(service, "_log_creation_usage", lambda **kwargs: None)
    kwargs = dict(system_prompt="system", user_prompt="original request")
    if entry in {"analysis", "json"}:
        result = asyncio.run(service.run_specialist_agent(agent_id="research", json_mode=entry == "json", **kwargs))
    elif entry == "skill":
        result = "".join(asyncio.run(_collect_stream(service.stream_specialist_agent(agent_id="skill", **kwargs))))
    else:
        result = "".join(asyncio.run(_collect_stream(service.stream_agent_document(**kwargs))))
    assert result == ('{"complete":true}' if entry == "json" else "Complete document.")
    assert len(calls) == 2
    assert calls[1]["num_predict"] > calls[0]["num_predict"]
    assert all(call["disable_thinking"] for call in calls)
    assert all(call["user_prompt"] == "original request" for call in calls)


def test_agent_length_recovery_stops_at_budget_ceiling(monkeypatch):
    from creation.operations import OperationError
    service = object.__new__(CreationService)
    budgets = []
    async def generate(**kwargs):
        budgets.append(kwargs["num_predict"])
        yield "partial"
        raise OperationError("CREATION_DOCUMENT_TRUNCATED", "length")
    monkeypatch.setattr(service, "_stream_direct_completion", generate)
    emitted = []
    async def run():
        with pytest.raises(OperationError, match="length"):
            async for chunk in service._stream_complete_agent_output(num_predict=1600):
                emitted.append(chunk)
    asyncio.run(run())
    assert budgets == [1600, 6400, 16384]
    assert emitted == []

@pytest.mark.parametrize("code,key", [("CREATION_DOCUMENT_INVALID", None), ("CREATION_DOCUMENT_TRUNCATED", "test-key")])
def test_agent_budget_recovery_does_not_retry_unrelated_or_external_errors(monkeypatch, code, key):
    from creation.operations import OperationError
    service = object.__new__(CreationService)
    calls = []
    async def generate(**kwargs):
        calls.append(kwargs)
        yield "partial"
        raise OperationError(code, "rejected")
    monkeypatch.setattr(service, "_stream_direct_completion", generate)
    with pytest.raises(OperationError, match="rejected"):
        asyncio.run(_collect_stream(service._stream_complete_agent_output(num_predict=1600, creation_api_key=key)))
    assert len(calls) == 1
