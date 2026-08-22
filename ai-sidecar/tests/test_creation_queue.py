from __future__ import annotations

import concurrent.futures
import importlib
import json
import threading
from typing import Optional

import httpx
import pytest

from inference_queue import LANE_P0_CREATION, Priority

creation_app = importlib.import_module("creation.app")


def parse_sse_events(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_creation_generate_runs_in_interactive_p0_lane(monkeypatch):
    calls: list[tuple[Priority, Optional[str]]] = []

    async def fake_generate_document(**_kwargs):
        yield "创作"
        yield "完成"

    class ThreadQueue:
        def submit(self, priority, fn, lane=None):
            calls.append((priority, lane))
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            return future

    monkeypatch.setattr(
        creation_app.creation_service,
        "generate_document",
        fake_generate_document,
    )
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())

    transport = httpx.ASGITransport(app=creation_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creation/generate",
            json={
                "user_prompt": "生成一份方案",
                "design_templates": [],
                "enable_rag": False,
            },
        )

    assert response.status_code == 200
    assert calls == [(Priority.P0, LANE_P0_CREATION)]
    assert '"content": "\\u521b\\u4f5c"' in response.text
    assert '"content": "\\u5b8c\\u6210"' in response.text
    assert '"done": true' in response.text


@pytest.mark.asyncio
async def test_creation_agent_loop_runs_in_interactive_p0_lane(monkeypatch):
    calls: list[tuple[Priority, Optional[str]]] = []

    async def fake_agent_run(**_kwargs):
        yield {
            "schema_version": "creation.agent.v1",
            "event_id": "event-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "sequence": 1,
            "timestamp": 1,
            "type": "run.completed",
            "status": "completed",
            "actor": {
                "kind": "agent",
                "id": "creation_main_agent",
                "name": "创作 Agent",
            },
            "summary": "本轮创作完成",
            "goal": {"status": "complete"},
            "environment_patch": {},
            "data": {"document": "# 方案"},
        }

    class ThreadQueue:
        def submit(self, priority, fn, lane=None):
            calls.append((priority, lane))
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            return future

    monkeypatch.setattr(creation_app.creation_agent_loop, "run", fake_agent_run)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())

    transport = httpx.ASGITransport(app=creation_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creation/agent/run",
            json={
                "user_prompt": "生成一份方案",
                "design_templates": [],
                "enable_rag": False,
                "model_mode": "external",
                "conversation": [{"role": "user", "content": "生成一份方案"}],
            },
        )

    assert response.status_code == 200
    assert calls == [(Priority.P0, LANE_P0_CREATION)]
    events = parse_sse_events(response)
    assert events[0]["type"] == "run.queued"
    assert events[0]["status"] == "waiting"
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["document"] == "# 方案"


@pytest.mark.asyncio
async def test_creation_agent_loop_failure_keeps_event_contract(monkeypatch):
    async def failing_agent_run(**_kwargs):
        if False:
            yield {}
        raise RuntimeError("测试失败")

    class ThreadQueue:
        def submit(self, _priority, fn, lane=None):
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            return future

    monkeypatch.setattr(creation_app.creation_agent_loop, "run", failing_agent_run)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())

    transport = httpx.ASGITransport(app=creation_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creation/agent/run",
            json={
                "user_prompt": "生成一份失败处理方案",
                "design_templates": [],
                "enable_rag": False,
                "session_id": "session-failure",
                "run_id": "run-failure",
            },
        )

    assert response.status_code == 200
    events = parse_sse_events(response)
    assert events[0]["type"] == "run.queued"
    event = events[-1]
    assert event["type"] == "run.failed"
    assert event["session_id"] == "session-failure"
    assert event["run_id"] == "run-failure"
    assert event["actor"]["id"] == "creation_main_agent"
    assert event["goal"]["status"] == "failed"


@pytest.mark.asyncio
async def test_creation_agent_transport_failure_returns_retryable_user_message(monkeypatch):
    async def failing_agent_run(**_kwargs):
        if False:
            yield {}
        raise httpx.ConnectError("")

    class ThreadQueue:
        def submit(self, _priority, fn, lane=None):
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            return future

    monkeypatch.setattr(creation_app.creation_agent_loop, "run", failing_agent_run)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())

    transport = httpx.ASGITransport(app=creation_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creation/agent/run",
            json={
                "user_prompt": "生成一份方案",
                "design_templates": [],
                "enable_rag": False,
            },
        )

    event = parse_sse_events(response)[-1]
    assert event["type"] == "run.failed"
    assert event["summary"] == "模型服务连接中断，已重试仍未恢复，可稍后重试"
    assert event["data"] == {
        "error_code": "MODEL_TRANSPORT_UNAVAILABLE",
        "retryable": True,
    }
