from __future__ import annotations

import concurrent.futures

import model_api_server
from knowledge.extractor_v2 import (
    BakeModelRequestError,
    BakeModelTransportError,
    BakeOutputTruncatedError,
)


class _TimeoutQueue:
    def __init__(self):
        self.timeout = None

    def submit_sync(self, *_args, **kwargs):
        self.timeout = kwargs.get("timeout")
        raise concurrent.futures.TimeoutError


class _Extractor:
    def __init__(self, prompt_tokens):
        self.prompt_tokens = prompt_tokens

    def estimate_bake_bundle_prompt_tokens(self, _candidate):
        return self.prompt_tokens

    def estimate_merge_document_prompt_tokens(self, _existing_document, _candidate):
        return self.prompt_tokens


def test_bake_extract_forwards_retry_attempt(monkeypatch):
    class _ExecutingQueue:
        def submit_sync(self, _priority, fn, **_kwargs):
            return fn()

    class _RecordingExtractor(_Extractor):
        def __init__(self):
            super().__init__(1_000)
            self.retry_attempt = None
            self.retry_error_code = None

        def extract_bake_bundle(
            self,
            _candidate,
            *,
            preempt_check,
            retry_attempt,
            retry_error_code,
        ):
            self.retry_attempt = retry_attempt
            self.retry_error_code = retry_error_code
            assert preempt_check is not None
            rejected = {"accepted": False, "reason": "test", "payload": None}
            return {
                "knowledge": rejected,
                "design": rejected,
                "sop": rejected,
            }

    extractor = _RecordingExtractor()
    monkeypatch.setattr(model_api_server, "get_global_queue", _ExecutingQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: extractor)

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={
            "retry_attempt": 2,
            "retry_error_code": "INFERENCE_TIMEOUT",
            "candidate": {"source_timeline_id": 42},
        },
    )

    assert response.status_code == 200
    assert extractor.retry_attempt == 2
    assert extractor.retry_error_code == "INFERENCE_TIMEOUT"


def test_bake_extract_timeout_is_retryable(monkeypatch):
    queue = _TimeoutQueue()
    monkeypatch.setattr(model_api_server, "get_global_queue", lambda: queue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 504
    assert response.get_json() == {
        "error": "bake 提炼超时，任务已取消",
        "code": "INFERENCE_TIMEOUT",
        "retryable": True,
        "scope": "candidate",
    }
    assert queue.timeout == 180.0


def test_bake_extract_truncated_output_is_structured_and_retryable(monkeypatch):
    class _TruncatedQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise BakeOutputTruncatedError(
                "bake bundle output invalid: truncated_json"
            )

    monkeypatch.setattr(model_api_server, "get_global_queue", _TruncatedQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(24_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": "烘焙输出不符合结构要求",
        "code": "BAKE_OUTPUT_TRUNCATED",
        "retryable": True,
        "scope": "candidate",
    }


def test_bake_extract_model_bad_request_is_terminal_and_sanitized(monkeypatch):
    class _RejectedQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise BakeModelRequestError(
                400,
                '{"error":"failed to parse grammar","model":"provider-model"}',
            )

    monkeypatch.setattr(model_api_server, "get_global_queue", _RejectedQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": "本地模型拒绝了烘焙请求",
        "code": "BAKE_MODEL_REQUEST_INVALID",
        "retryable": False,
        "scope": "candidate",
    }


def test_bake_extract_model_5xx_is_bounded_candidate_error(monkeypatch):
    class _RejectedQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise BakeModelRequestError(500, '{"model":"provider-model"}')

    monkeypatch.setattr(model_api_server, "get_global_queue", _RejectedQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 502
    assert response.get_json() == {
        "error": "本地模型执行烘焙请求失败",
        "code": "BAKE_MODEL_UPSTREAM_ERROR",
        "retryable": True,
        "scope": "candidate",
    }


def test_bake_extract_missing_model_is_explicit_service_error(monkeypatch):
    class _MissingModelQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise BakeModelRequestError(404, '{"model":"provider-model"}')

    monkeypatch.setattr(model_api_server, "get_global_queue", _MissingModelQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "本地模型服务配置不可用",
        "code": "MODEL_UNAVAILABLE",
        "retryable": True,
        "scope": "service",
    }


def test_bake_extract_transport_failure_is_explicit_service_error(monkeypatch):
    class _UnavailableQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise BakeModelTransportError("connection refused")

    monkeypatch.setattr(model_api_server, "get_global_queue", _UnavailableQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "本地模型服务暂不可用",
        "code": "MODEL_UNAVAILABLE",
        "retryable": True,
        "scope": "service",
    }


def test_bake_extract_unclassified_exception_is_bounded_and_sanitized(monkeypatch):
    class _BrokenQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise ValueError("provider-model secret response")

    monkeypatch.setattr(model_api_server, "get_global_queue", _BrokenQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={"candidate": {"source_timeline_id": 42}},
    )

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "烘焙提炼内部处理失败",
        "code": "BAKE_INTERNAL_ERROR",
        "retryable": True,
        "scope": "candidate",
    }


def test_bake_extract_invalid_retry_attempt_is_structured_4xx(monkeypatch):
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(12_000))

    response = model_api_server.app.test_client().post(
        "/bake/extract",
        json={
            "retry_attempt": "not-an-integer",
            "candidate": {"source_timeline_id": 42},
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "retry_attempt 必须是非负整数",
        "code": "BAKE_REQUEST_INVALID",
        "retryable": False,
        "scope": "candidate",
    }


def test_bake_document_merge_timeout_is_retryable(monkeypatch):
    queue = _TimeoutQueue()
    monkeypatch.setattr(model_api_server, "get_global_queue", lambda: queue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(24_000))

    response = model_api_server.app.test_client().post(
        "/bake/merge_document",
        json={
            "existing_document": {"title": "existing"},
            "candidate": {"source_timeline_id": 42},
        },
    )

    assert response.status_code == 504
    assert response.get_json() == {
        "error": "bake 文档合并超时，任务已取消",
        "code": "INFERENCE_TIMEOUT",
        "retryable": True,
        "scope": "candidate",
    }
    assert queue.timeout == 300.0


def test_bake_document_merge_unclassified_exception_is_bounded(monkeypatch):
    class _BrokenQueue:
        def submit_sync(self, *_args, **_kwargs):
            raise RuntimeError("deterministic merge bug")

    monkeypatch.setattr(model_api_server, "get_global_queue", _BrokenQueue)
    monkeypatch.setattr(model_api_server, "get_bake_extractor", lambda: _Extractor(24_000))

    response = model_api_server.app.test_client().post(
        "/bake/merge_document",
        json={
            "existing_document": {"title": "existing"},
            "candidate": {"source_timeline_id": 42},
        },
    )

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "烘焙文档合并内部处理失败",
        "code": "BAKE_INTERNAL_ERROR",
        "retryable": True,
        "scope": "candidate",
    }


def test_bake_document_merge_timeout_before_budget_is_still_structured(monkeypatch):
    def _raise_timeout():
        raise concurrent.futures.TimeoutError

    monkeypatch.setattr(model_api_server, "get_bake_extractor", _raise_timeout)

    response = model_api_server.app.test_client().post(
        "/bake/merge_document",
        json={
            "existing_document": {"title": "existing"},
            "candidate": {"source_timeline_id": 42},
        },
    )

    assert response.status_code == 504
    assert response.get_json() == {
        "error": "bake 文档合并超时，任务已取消",
        "code": "INFERENCE_TIMEOUT",
        "retryable": True,
        "scope": "candidate",
    }


def test_bake_timeout_budget_uses_prompt_size():
    assert model_api_server.bake_inference_timeout_seconds(19_999) == 180.0
    assert model_api_server.bake_inference_timeout_seconds(20_000) == 300.0
