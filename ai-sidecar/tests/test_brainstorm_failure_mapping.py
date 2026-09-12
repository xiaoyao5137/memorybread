"""Exhausted brainstorm failures retain a safe, actionable HTTP contract."""

import asyncio
import concurrent.futures
import importlib
import logging

import httpx
import pytest

from creation.operations import OperationError
from creation.service import CloudModelRequestError


creation_app = importlib.import_module("creation.app")
PRIVATE_DETAIL = "provider-secret=sk-private model=private-model"
PRIVATE_URL = "https://private.example/model?api_key=sk-private"
SAFE_FAILURES = {
    "BRAINSTORM_MODEL_TIMEOUT": (
        504, "脑暴问题生成超时，已保留当前输入，请稍后重试", True,
    ),
    "MODEL_RATE_LIMITED": (
        429, "模型服务当前繁忙，已保留当前输入，请稍后重试", True,
    ),
    "MODEL_SERVICE_UNAVAILABLE": (
        503, "脑暴问题生成服务暂时不可用，已保留当前输入，请稍后重试", True,
    ),
    "MODEL_ACCESS_DENIED": (
        403, "当前模型访问未获授权，请切换可用模型或检查模型权限后重试", False,
    ),
    "MODEL_REQUEST_FAILED": (
        502, "模型未能接受脑暴请求，请检查模型配置后重试", False,
    ),
    "BRAINSTORM_MODEL_OUTPUT_INVALID": (
        502, "脑暴问题未生成有效结果，已保留当前输入，请重试", False,
    ),
}


async def failed_brainstorm(monkeypatch, error):
    class FailedQueue:
        def submit(self, *_args, **_kwargs):
            future = concurrent.futures.Future()
            future.set_exception(error)
            return future

    monkeypatch.setattr(creation_app, "get_global_queue", lambda: FailedQueue())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test"
    ) as client:
        return await client.post(
            "/creation/brainstorm/next", json={"root_request": "设计一份方案"},
        )


def assert_safe_failure(response, caplog, expected_code, error):
    status, message, retryable = SAFE_FAILURES[expected_code]
    assert response.status_code == status
    assert response.json() == {"detail": {
        "code": expected_code, "message": message, "retryable": retryable,
    }}
    records = [record for record in caplog.records if record.name == creation_app.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "Dynamic brainstorm generation failed: type="
        + type(error).__name__ + " code=" + expected_code
    )
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    for private_value in (PRIVATE_DETAIL, PRIVATE_URL, "sk-private", "private-model"):
        assert private_value not in response.text
        assert private_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["local", "cloud"])
@pytest.mark.parametrize("upstream_status,expected_code", [
    (408, "BRAINSTORM_MODEL_TIMEOUT"),
    (504, "BRAINSTORM_MODEL_TIMEOUT"),
    (429, "MODEL_RATE_LIMITED"),
    (500, "MODEL_SERVICE_UNAVAILABLE"),
    (501, "MODEL_SERVICE_UNAVAILABLE"),
    (502, "MODEL_SERVICE_UNAVAILABLE"),
    (503, "MODEL_SERVICE_UNAVAILABLE"),
    (401, "MODEL_ACCESS_DENIED"),
    (403, "MODEL_ACCESS_DENIED"),
    (400, "MODEL_REQUEST_FAILED"),
    (404, "MODEL_REQUEST_FAILED"),
    (409, "MODEL_REQUEST_FAILED"),
    (422, "MODEL_REQUEST_FAILED"),
])
async def test_brainstorm_http_failures_are_safe_and_preserve_status_category(
    monkeypatch, caplog, kind, upstream_status, expected_code,
):
    caplog.set_level(logging.WARNING, logger=creation_app.__name__)
    request = httpx.Request("POST", PRIVATE_URL)
    error = (
        httpx.HTTPStatusError(
            PRIVATE_DETAIL, request=request,
            response=httpx.Response(upstream_status, request=request, text=PRIVATE_DETAIL),
        )
        if kind == "local" else CloudModelRequestError(upstream_status, PRIVATE_DETAIL)
    )
    response = await failed_brainstorm(monkeypatch, error)
    assert_safe_failure(response, caplog, expected_code, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,expected_code", [
    (httpx.ConnectError, "MODEL_SERVICE_UNAVAILABLE"),
    (httpx.RemoteProtocolError, "MODEL_SERVICE_UNAVAILABLE"),
    (httpx.ReadTimeout, "BRAINSTORM_MODEL_TIMEOUT"),
    (httpx.ConnectTimeout, "BRAINSTORM_MODEL_TIMEOUT"),
    (RuntimeError, "BRAINSTORM_MODEL_OUTPUT_INVALID"),
])
async def test_brainstorm_transport_and_unknown_failures_do_not_log_exception_text(
    monkeypatch, caplog, error_type, expected_code,
):
    caplog.set_level(logging.WARNING, logger=creation_app.__name__)
    error = error_type(PRIVATE_DETAIL + " " + PRIVATE_URL)
    response = await failed_brainstorm(monkeypatch, error)
    assert_safe_failure(response, caplog, expected_code, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_code,expected_code", [
    ("MODEL_TIMEOUT", "BRAINSTORM_MODEL_TIMEOUT"),
    ("BRAINSTORM_MODEL_TIMEOUT", "BRAINSTORM_MODEL_TIMEOUT"),
    ("MODEL_RATE_LIMITED", "MODEL_RATE_LIMITED"),
    ("MODEL_UNAVAILABLE", "MODEL_SERVICE_UNAVAILABLE"),
    ("MODEL_SERVICE_UNAVAILABLE", "MODEL_SERVICE_UNAVAILABLE"),
    ("MODEL_TRANSPORT_UNAVAILABLE", "MODEL_SERVICE_UNAVAILABLE"),
    ("MODEL_ACCESS_DENIED", "MODEL_ACCESS_DENIED"),
    ("MODEL_REQUEST_FAILED", "MODEL_REQUEST_FAILED"),
    (PRIVATE_DETAIL, "BRAINSTORM_MODEL_OUTPUT_INVALID"),
])
async def test_brainstorm_operation_codes_are_allowlisted_and_messages_are_fixed(
    monkeypatch, caplog, operation_code, expected_code,
):
    caplog.set_level(logging.WARNING, logger=creation_app.__name__)
    error = OperationError(operation_code, PRIVATE_DETAIL + " " + PRIVATE_URL)
    response = await failed_brainstorm(monkeypatch, error)
    assert_safe_failure(response, caplog, expected_code, error)


@pytest.mark.asyncio
async def test_brainstorm_asyncio_timeout_retains_existing_safe_deadline_contract(
    monkeypatch, caplog,
):
    response = await failed_brainstorm(
        monkeypatch, asyncio.TimeoutError(PRIVATE_DETAIL + " " + PRIVATE_URL),
    )
    assert response.status_code == 504
    assert response.json() == {"detail": {
        "code": "BRAINSTORM_MODEL_TIMEOUT",
        "message": "本轮脑暴生成等待超时，已保留当前输入和已确认进度，请稍后重试",
        "retryable": True,
    }}
    assert "sk-private" not in response.text
    assert "sk-private" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
