from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import json
import threading
from types import SimpleNamespace
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
async def test_dynamic_brainstorm_runs_in_interactive_p0_lane(monkeypatch):
    calls: list[tuple[Priority, Optional[str]]] = []
    received = {}

    async def fake_next_step(**kwargs):
        received.update(kwargs)
        return {
            "status": "question",
            "readiness_reason": "仍需继续下钻",
            "open_flags": ["部署边界"],
            "question": {
                "id": "q_dynamic",
                "dimension": "私有化部署下钻",
                "type": "single_choice",
                "prompt": "私有化部署首先适配哪类基础设施？",
                "why_now": "它会改变架构边界。",
                "required": True,
                "allow_custom": True,
                "answer_template": "补充现状。",
                "options": [],
            },
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

    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", fake_next_step)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: ThreadQueue())

    transport = httpx.ASGITransport(app=creation_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creation/brainstorm/next",
            json={
                "root_request": "设计知识库方案",
                "decisions": [{"answer": "私有化部署"}],
                "brief_markdown": "# 创作简报",
                "selected_skills": [{"title": "微服务技术方案"}],
                "force_continue": True,
                "exploration_stage": "implementation",
            },
        )

    assert response.status_code == 200
    assert calls == [(Priority.P0, LANE_P0_CREATION)]
    assert response.json()["question"]["dimension"] == "私有化部署下钻"
    assert received["decisions"][0]["answer"] == "私有化部署"
    assert received["selected_skills"][0]["title"] == "微服务技术方案"
    assert received["force_continue"] is True
    assert received["exploration_stage"] == "implementation"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name, expected_code", [
    ("BrainstormGenerationError", "BRAINSTORM_MODEL_OUTPUT_INVALID"),
    ("BrainstormOutputTruncated", "BRAINSTORM_MODEL_OUTPUT_TRUNCATED"),
    ("BrainstormGenerationTimeout", "BRAINSTORM_MODEL_TIMEOUT"),
])
async def test_brainstorm_failure_preserves_stage_error_contract(monkeypatch, error_name, expected_code):
    from creation import brainstorm

    class FailedQueue:
        def submit(self, *args, **kwargs):
            future = concurrent.futures.Future()
            future.set_exception(getattr(brainstorm, error_name)("脑暴选项未能完整生成"))
            return future

    monkeypatch.setattr(creation_app, "get_global_queue", lambda: FailedQueue())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test"
    ) as client:
        response = await client.post("/creation/brainstorm/next", json={"root_request": "设计一份方案"})
    assert response.status_code == (504 if expected_code == "BRAINSTORM_MODEL_TIMEOUT" else 502)
    assert response.json()["detail"] == {
        "code": expected_code, "message": "脑暴选项未能完整生成",
        "retryable": expected_code == "BRAINSTORM_MODEL_TIMEOUT",
    }


@pytest.mark.asyncio
async def test_brainstorm_queue_deadline_cancels_unadmitted_work(monkeypatch):
    from creation.brainstorm import BrainstormGenerationTimeout

    calls = []

    async def fake_next_step(**kwargs):
        calls.append(kwargs)
        return {"status": "question"}

    class PendingQueue:
        def submit(self, *args, **kwargs):
            self.fn = args[1]
            self.future = concurrent.futures.Future()
            return self.future

    pending = PendingQueue()
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: pending)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "MAX_TURN_SECONDS", 0.02)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", fake_next_step)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test"
    ) as client:
        response = await client.post("/creation/brainstorm/next", json={"root_request": "设计一份方案"})

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "BRAINSTORM_MODEL_TIMEOUT"
    assert response.json()["detail"]["retryable"] is True
    assert pending.future.cancelled()
    assert not pending.future.set_running_or_notify_cancel()
    # 即使队列在取消/准入竞争中仍调用了函数，过期任务也不能触发模型。
    with pytest.raises(BrainstormGenerationTimeout):
        await asyncio.to_thread(pending.fn)
    assert calls == []


class BrainstormDeadlineThreadQueue:
    def submit(self, _priority, fn, lane=None):
        self.future = concurrent.futures.Future()

        def run():
            if not self.future.set_running_or_notify_cancel():
                return
            try:
                self.future.set_result(fn())
            except Exception as exc:
                self.future.set_exception(exc)

        self.worker = threading.Thread(target=run, name="brainstorm-deadline-test", daemon=True)
        self.worker.start()
        return self.future


@pytest.mark.asyncio
async def test_brainstorm_admission_uses_only_remaining_request_budget(monkeypatch):
    clock = [100.0]
    timeouts = []
    real_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        timeouts.append((threading.current_thread().name, timeout))
        return await real_wait_for(awaitable, timeout=timeout)

    async def fake_next_step(**kwargs):
        return {"status": "question"}

    class DelayedQueue(BrainstormDeadlineThreadQueue):
        def submit(self, *args, **kwargs):
            # 模拟入队后消耗 90 秒，无需让回归测试实际等待。
            clock[0] += 90.0
            return super().submit(*args, **kwargs)

    queue = DelayedQueue()
    monkeypatch.setattr(creation_app, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(creation_app.asyncio, "wait_for", record_wait_for)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "MAX_TURN_SECONDS", 150.0)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", fake_next_step)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test"
    ) as client:
        response = await client.post("/creation/brainstorm/next", json={"root_request": "设计一份方案"})

    assert response.status_code == 200
    assert ("brainstorm-deadline-test", 60.0) in timeouts
    assert all(timeout == 60.0 for _, timeout in timeouts)
    await asyncio.to_thread(queue.worker.join, 1.0)
    assert not queue.worker.is_alive()


@pytest.mark.asyncio
async def test_brainstorm_running_deadline_cancels_model_stream_and_finishes_worker(monkeypatch):
    from creation.brainstorm import BrainstormGenerationTimeout

    started = threading.Event()
    stream_closed = threading.Event()

    async def model_stream():
        started.set()
        try:
            await asyncio.sleep(60)
            yield "此内容不应生成"
        finally:
            stream_closed.set()

    async def fake_next_step(**kwargs):
        async for _ in model_stream():
            pass
        return {"status": "question"}

    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "MAX_TURN_SECONDS", 0.05)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", fake_next_step)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test"
    ) as client:
        response = await client.post("/creation/brainstorm/next", json={"root_request": "设计一份方案"})

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "BRAINSTORM_MODEL_TIMEOUT"
    assert response.json()["detail"]["retryable"] is True
    assert started.is_set()
    assert await asyncio.to_thread(stream_closed.wait, 1.0)
    await asyncio.to_thread(queue.worker.join, 1.0)
    assert not queue.worker.is_alive()
    assert isinstance(queue.future.exception(), BrainstormGenerationTimeout)


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


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_model_rejection_has_safe_actionable_failure_details(status):
    from creation.service import CloudModelRequestError
    code, summary, retryable = creation_app._creation_failure_details(
        CloudModelRequestError(status, "private provider model and request_id"))
    assert code == ("MODEL_ACCESS_DENIED" if status in {401, 403} else "MODEL_REQUEST_FAILED")
    assert "请" in summary
    assert "private" not in summary
    assert "request_id" not in summary
    assert retryable is False
