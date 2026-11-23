from __future__ import annotations

import ssl
import urllib.error

from rag.llm.cloud import CloudChatBackend


def test_configured_consultation_budget_reaches_custom_api(tmp_path, monkeypatch):
    import httpx
    import model_context
    import user_models

    monkeypatch.setattr(user_models, "profile_path", lambda: tmp_path / "models.json")
    saved = user_models.save_profile({
        "name": "Custom consultation",
        "model": "custom-chat",
        "base_url": "https://api.example.test/v1",
        "context_window_tokens": 32768,
    })
    assert saved["context_window_tokens"] == 32768
    backend = CloudChatBackend(saved["id"], "device-profile")
    assert backend.context_window_tokens == 32768
    requests = []

    async def post(self, url, *, headers, json):
        requests.append(json)
        return httpx.Response(200, json={"choices": [{"message": {"content": "回答"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    deltas = []
    backend.complete_stream("材料", system="答题要求", num_predict=1536, on_delta=deltas.append)
    assert deltas == ["回答"]
    assert requests[0]["max_tokens"] == 1536

    legacy = user_models.save_profile({**saved, "context_window_tokens": None})
    assert "context_window_tokens" not in legacy
    monkeypatch.setattr(model_context, "_api_window", lambda *args: 64000)
    assert CloudChatBackend(saved["id"], "device-profile").context_window_tokens == 64000


class _JsonResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


class _StreamResponse(_JsonResponse):
    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def _ssl_eof_error() -> urllib.error.URLError:
    return urllib.error.URLError(
        ssl.SSLEOFError(
            8,
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)",
        )
    )


def test_cloud_backend_retries_ssl_eof_and_succeeds(monkeypatch):
    calls = {"count": 0}

    def fake_urlopen(req, timeout=0):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _ssl_eof_error()
        return _JsonResponse(
            b'{"model":"qwen-plus","choices":[{"message":{"content":"ok"}}],"usage":{"completion_tokens":3}}'
        )

    monkeypatch.setattr("rag.llm.cloud.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("rag.llm.cloud.time.sleep", lambda _: None)

    backend = CloudChatBackend(model="qwen-plus", api_key="test-key", retries=2)
    result = backend.complete("hello", "system")

    assert result.text == "ok"
    assert result.tokens == 3
    assert calls["count"] == 2


def test_cloud_backend_reports_friendly_network_error_after_retries(monkeypatch):
    calls = {"count": 0}

    def fake_urlopen(req, timeout=0):
        calls["count"] += 1
        raise _ssl_eof_error()

    monkeypatch.setattr("rag.llm.cloud.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("rag.llm.cloud.time.sleep", lambda _: None)

    backend = CloudChatBackend(model="qwen-plus", api_key="test-key", retries=2)

    try:
        backend.complete("hello")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        message = str(exc)

    assert calls["count"] == 3
    assert "云端模型服务不可达" in message
    assert "网络或 TLS 连接被中断" in message
    assert "base_url" in message
    assert "urlopen error" not in message


def test_cloud_backend_streams_openai_compatible_deltas(monkeypatch):
    body = (
        'data: {"model":"brand","choices":[{"delta":{"content":"部分"},"finish_reason":null}]}\n\n'
        'data: {"model":"brand","choices":[{"delta":{"content":"答案"},"finish_reason":"stop"}],'
        '"usage":{"completion_tokens":4}}\n\n'
        'data: [DONE]\n\n'
    ).encode("utf-8")
    monkeypatch.setattr(
        "rag.llm.cloud.urllib.request.urlopen",
        lambda req, timeout=0: _StreamResponse(body),
    )
    backend = CloudChatBackend(model="brand", api_key="test-key")
    deltas: list[str] = []

    result = backend.complete_stream("hello", on_delta=deltas.append)

    assert deltas == ["部分", "答案"]
    assert result.text == "部分答案"
    assert result.tokens == 4
    assert result.done_reason == "stop"
