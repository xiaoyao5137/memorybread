"""Speculative brainstorm work must yield its real model resources to the user."""

import asyncio
import concurrent.futures
import importlib
import json
import threading

import httpx
import pytest

import inference_queue
from inference_queue import (
    InferenceQueue, LANE_P0_CREATION, LANE_P0_QUERY, LANE_P2_CREATION, Priority,
)
from tests.test_inference_transport import blocked_model

creation_app = importlib.import_module("creation.app")


@pytest.fixture
def isolated_queue(monkeypatch, tmp_path):
    # Never signal the user's live model processes during scheduling tests.
    for name in (
        "_INTERACTIVE_DEMAND_LOCK_FILE", "_INTERACTIVE_DEMAND_PROBE_LOCK_FILE",
        "_RAG_LOCK_FILE", "_RAG_LOCK_OWNER_FILE", "_GLOBAL_SLOT_PREFIX",
    ):
        monkeypatch.setattr(inference_queue, name, str(tmp_path / name.lower()))
    queue = InferenceQueue(max_concurrency=1, low_memory_threshold_mb=0)
    monkeypatch.setattr(creation_app, "get_global_queue", lambda: queue)
    yield queue
    queue.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch", [None, False, True])
async def test_prefetch_priority_and_programmatic_source_permission_gate(
    monkeypatch, isolated_queue, prefetch,
):
    async def next_step(**kwargs):
        task = inference_queue._WORKER_STATE.task
        assert task.priority == (Priority.P2 if prefetch else Priority.P0)
        assert task.lane == (LANE_P2_CREATION if prefetch else LANE_P0_CREATION)
        assert kwargs["prefetch"] is bool(prefetch)
        assert kwargs["question_batch_limit"] == 3
        assert kwargs["extension_goal"] == "如何安排独立的画面检查？"
        assert kwargs["sibling_question_context"] == ["如何设计对白节奏？"]
        return {
            "status": "question",
            "prefetch_safe_option_ids": ["denied", "grant", "fabricated"],
            "question": {"options": [
                {"id": "normal", "label": "优先试验关键步骤"},
                {"id": "denied", "label": "不要使用本地资料"},
                {"id": "grant", "label": "可以使用本地资料"},
                {"id": "", "label": "无效编号"},
            ]},
        }

    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", next_step)
    payload = {"root_request": "生成一份活动方案", "question_batch_limit": 3,
               "extension_goal": "如何安排独立的画面检查？",
               "sibling_question_context": ["如何设计对白节奏？"]}
    if prefetch is not None:
        payload["prefetch"] = prefetch
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test",
    ) as client:
        response = await client.post("/creation/brainstorm/next", json=payload)
    assert response.status_code == 200
    assert response.json()["prefetch_safe_option_ids"] == ["normal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", [LANE_P0_QUERY, LANE_P0_CREATION])
@pytest.mark.parametrize("phase", ["headers", "stream"])
async def test_prefetch_preemption_closes_model_socket_before_foreground_runs(
    monkeypatch, isolated_queue, blocked_model, lane, phase,
):
    url, started, disconnected, _ = blocked_model(phase)
    closed = threading.Event()

    async def next_step(**kwargs):
        assert inference_queue._WORKER_STATE.task.priority == Priority.P2
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                async with client.stream("POST", url, json={"stream": True}) as response:
                    async for _ in response.aiter_lines():
                        pass
        finally:
            closed.set()
        return {"status": "question"}

    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", next_step)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=creation_app.app), base_url="http://test",
    ) as client:
        prefetch = asyncio.create_task(client.post("/creation/brainstorm/next", json={
            "root_request": "预生成下一道题", "prefetch": True,
        }))
        assert await asyncio.to_thread(started.wait, 2)
        foreground = isolated_queue.submit(Priority.P0, closed.is_set, lane=lane)
        assert await asyncio.wait_for(asyncio.wrap_future(foreground), 2) is True
        response = await asyncio.wait_for(prefetch, 2)
    assert await asyncio.to_thread(disconnected.wait, 2)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "INFERENCE_PREEMPTED"
    assert response.json()["detail"]["retryable"] is True
    assert isolated_queue.stats()["totals"]["P2"]["preempted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch", [False, True])
async def test_cancelled_brainstorm_closes_running_coroutine(
    monkeypatch, isolated_queue, prefetch,
):
    started, closed = threading.Event(), threading.Event()

    async def next_step(**kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", next_step)
    task = asyncio.create_task(creation_app.next_brainstorm_step(
        creation_app.BrainstormNextRequest(root_request="保留输入", prefetch=prefetch),
    ))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    foreground = isolated_queue.submit(Priority.P0, closed.is_set, lane=LANE_P0_QUERY)
    assert await asyncio.wait_for(asyncio.wrap_future(foreground), 2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_core_http_disconnect_cancels_prefetch_without_waiting_for_deadline(
    monkeypatch, isolated_queue, queued,
):
    started, closed = threading.Event(), threading.Event()
    pending_future = concurrent.futures.Future()

    async def next_step(**kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    class PendingQueue:
        def submit(self, *args, **kwargs):
            started.set()
            return pending_future

    if queued:
        monkeypatch.setattr(creation_app, "get_global_queue", lambda: PendingQueue())
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "next_step", next_step)
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": json.dumps({
        "root_request": "过期方向", "prefetch": True,
    }).encode(), "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(creation_app.app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/creation/brainstorm/next",
        "raw_path": b"/creation/brainstorm/next", "query_string": b"",
        "root_path": "", "headers": [(b"content-type", b"application/json")],
        "server": ("test", 80), "client": ("test", 1),
    }, messages.get, send))
    assert await asyncio.to_thread(started.wait, 2)
    messages.put_nowait({"type": "http.disconnect"})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    if queued:
        assert pending_future.cancelled()
    else:
        assert await asyncio.to_thread(closed.wait, 2)
    assert not sent


@pytest.mark.asyncio
async def test_slow_prefetch_retrieval_does_not_hold_model_slot_after_preemption(
    monkeypatch, isolated_queue,
):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    generated = []

    def retrieve(*args):
        started.set()
        try:
            release.wait(5)
            return {"status": "skipped", "sources": []}
        finally:
            finished.set()

    async def complete(*args, **kwargs):
        generated.append(True)
        raise AssertionError("cancelled retrieval must never enter the model")

    monkeypatch.setattr(creation_app.brainstorm_coordinator, "_retrieve_memory", retrieve)
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "_complete", complete)
    task = asyncio.create_task(creation_app.next_brainstorm_step(
        creation_app.BrainstormNextRequest(
            root_request="生成活动方案", prefetch=True, exploration_stage="solutions",
        ),
    ))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        foreground = isolated_queue.submit(Priority.P0, lambda: not finished.is_set())
        assert await asyncio.wait_for(asyncio.wrap_future(foreground), 2) is True
        with pytest.raises(creation_app.HTTPException) as error:
            await asyncio.wait_for(task, 2)
        assert error.value.detail["code"] == "INFERENCE_PREEMPTED"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert generated == []


@pytest.mark.asyncio
async def test_prefetch_queue_timeout_withdraws_unstarted_work(monkeypatch):
    future = concurrent.futures.Future()

    class PendingQueue:
        def submit(self, priority, fn, lane):
            assert (priority, lane) == (Priority.P2, LANE_P2_CREATION)
            return future

    monkeypatch.setattr(creation_app, "get_global_queue", lambda: PendingQueue())
    monkeypatch.setattr(creation_app.brainstorm_coordinator, "MAX_TURN_SECONDS", 0.01)
    with pytest.raises(creation_app.HTTPException) as error:
        await creation_app.next_brainstorm_step(creation_app.BrainstormNextRequest(
            root_request="预生成下一题", prefetch=True,
        ))
    assert error.value.status_code == 504
    assert future.cancelled()


@pytest.mark.asyncio
async def test_cancelled_prefetch_retrievals_do_not_accumulate_executor_jobs(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def retrieve(*args):
        calls.append(True)
        started.set()
        release.wait(5)
        return {"status": "skipped", "sources": []}

    coordinator = creation_app.brainstorm_coordinator
    monkeypatch.setattr(coordinator, "_retrieve_memory", retrieve)
    first = asyncio.create_task(coordinator._retrieve_prefetch_memory())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(10):
            obsolete = asyncio.create_task(coordinator._retrieve_prefetch_memory())
            await asyncio.sleep(0)
            obsolete.cancel()
            with pytest.raises(asyncio.CancelledError):
                await obsolete
        assert calls == [True]
    finally:
        release.set()
        assert (await asyncio.wait_for(first, 2))["status"] == "skipped"
    assert calls == [True]
