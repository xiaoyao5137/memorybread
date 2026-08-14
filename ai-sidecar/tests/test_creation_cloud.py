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
