import asyncio
import importlib
import threading
from types import SimpleNamespace

import httpx
import pytest

from creation.brainstorm_stages import BrainstormStageCoordinator
from tests.test_creation_queue import BrainstormDeadlineThreadQueue


@pytest.mark.parametrize("raw,expected", [
    (None, 210), ("", 210), (" 90 ", 90), ("30", 30), ("600", 600),
    ("29", 210), ("601", 210), ("90.5", 210), ("+90", 210),
    ("9_0", 210), ("unknown", 210), ("NaN", 210),
])
def test_stage_budget_configuration_matches_core_contract(monkeypatch, raw, expected):
    key = "MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS"
    if raw is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, raw)
    assert BrainstormStageCoordinator.batch_timeout_seconds() == expected


@pytest.mark.asyncio
async def test_stage_queue_uses_stage_budget_and_deducts_admission_wait(monkeypatch):
    app = importlib.import_module("creation.app")
    clock = [100.0]
    timeouts = []
    real_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        timeouts.append((threading.current_thread().name, timeout))
        return await real_wait_for(awaitable, timeout=timeout)

    async def stage_batch(**kwargs):
        return {"topics": [], "chapters": [], "open_flags": []}

    class DelayedQueue(BrainstormDeadlineThreadQueue):
        def submit(self, *args, **kwargs):
            clock[0] += 90
            return super().submit(*args, **kwargs)

    queue = DelayedQueue()
    monkeypatch.setenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", "210")
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(app.asyncio, "wait_for", record_wait_for)
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    monkeypatch.setattr(app.brainstorm_coordinator, "MAX_TURN_SECONDS", 150.0)
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json={
            "root_request": "设计周报", "stage": "direction", "kind": "plan"})
    assert response.status_code == 200
    assert ("brainstorm-deadline-test", 120.0) in timeouts
    assert all(timeout == 120.0 for _, timeout in timeouts)
    await asyncio.to_thread(queue.worker.join, 1.0)
    assert not queue.worker.is_alive()


@pytest.mark.asyncio
async def test_stage_queue_deadline_cancels_running_work_and_releases_worker(monkeypatch):
    app = importlib.import_module("creation.app")
    closed = threading.Event()

    async def stage_batch(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()

    queue = BrainstormDeadlineThreadQueue()
    monkeypatch.delenv("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(BrainstormStageCoordinator, "MAX_TURN_SECONDS", 0.03)
    monkeypatch.setattr(app.brainstorm_coordinator, "MAX_TURN_SECONDS", 150.0)
    monkeypatch.setattr(app.brainstorm_stage_coordinator, "stage_batch", stage_batch)
    monkeypatch.setattr(app, "get_global_queue", lambda: queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/creation/brainstorm/stage/batch", json={
            "root_request": "设计周报", "stage": "direction", "kind": "questions"})
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "BRAINSTORM_MODEL_TIMEOUT"
    assert await asyncio.to_thread(closed.wait, 1.0)
    await asyncio.to_thread(queue.worker.join, 1.0)
    assert not queue.worker.is_alive()
