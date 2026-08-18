"""inference_queue 的功能测试。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from types import SimpleNamespace
from unittest import mock

import pytest

import inference_queue as inference_queue_module
from inference_queue import (
    InferenceQueue,
    InferencePreemptedError,
    LANE_P0_QUERY,
    LANE_P1_CAPTURE,
    LANE_P1_PREEXTRACT,
    LANE_P2_BAKE,
    Priority,
    QueueEvictedError,
    interactive_demand_active,
    raise_if_preempted,
)


@pytest.fixture
def small_queue():
    q = InferenceQueue(
        per_priority_limit=3,
        total_limit=10,
        low_memory_threshold_mb=10,
        max_concurrency=1,
    )
    yield q
    q.shutdown()


def _delayed_factory(order: list, label: str, delay: float = 0.02):
    def fn():
        time.sleep(delay)
        order.append(label)
        return label
    return fn


def test_priority_order_p0_beats_p2(small_queue):
    """P0 到达后，正在执行的 P2 必须主动让出，而不是等它自然完成。"""
    order: list[str] = []
    started = time.monotonic()

    def background():
        while True:
            raise_if_preempted()
            time.sleep(0.01)

    long_fut = small_queue.submit(Priority.P2, background)
    time.sleep(0.05)
    p0_fut = small_queue.submit(
        Priority.P0,
        _delayed_factory(order, "P0", 0.01),
    )

    assert p0_fut.result(timeout=1) == "P0"
    assert isinstance(long_fut.exception(timeout=1), InferencePreemptedError)
    assert order == ["P0"]
    assert time.monotonic() - started < 0.5
    assert small_queue.stats()["totals"]["P2"]["preempted"] == 1
    assert small_queue.stats()["background_retry_after_ms"] > 0


def test_p1_waits_for_running_p2_without_preemption(small_queue):
    """P1 只在等待队列中优先，不能抢占已运行的 P2，避免重复计算降低吞吐。"""
    order: list[str] = []
    p2_started = threading.Event()

    def background():
        p2_started.set()
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            raise_if_preempted()
            time.sleep(0.005)
        order.append("P2")
        return "P2"

    p2_future = small_queue.submit(Priority.P2, background)
    assert p2_started.wait(timeout=0.5)
    p1_future = small_queue.submit(
        Priority.P1,
        _delayed_factory(order, "P1", 0.01),
    )

    assert p2_future.result(timeout=1) == "P2"
    assert p1_future.result(timeout=1) == "P1"
    assert order == ["P2", "P1"]
    assert small_queue.stats()["totals"]["P2"]["preempted"] == 0


def test_is_idle_tracks_queued_and_running_tasks(small_queue):
    assert small_queue.is_idle() is True

    fut = small_queue.submit(Priority.P2, _delayed_factory([], "work", 0.1))
    assert small_queue.is_idle() is False

    fut.result(timeout=5)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if small_queue.is_idle():
            break
        time.sleep(0.01)

    assert small_queue.is_idle() is True


def test_submit_sync_timeout_preempts_active_background_task(small_queue):
    """调用方超时后，活动任务必须释放槽位，不能继续幽灵运行。"""
    started = threading.Event()

    def background():
        started.set()
        while True:
            raise_if_preempted()
            time.sleep(0.005)

    with pytest.raises(FutureTimeoutError):
        small_queue.submit_sync(Priority.P2, background, timeout=0.05)

    assert started.wait(timeout=0.5)
    assert small_queue.submit_sync(
        Priority.P2,
        lambda: "next",
        timeout=1,
    ) == "next"
    stats = small_queue.stats()["totals"]["P2"]
    assert stats["timed_out"] == 1
    assert stats["preempted"] == 1


def test_per_priority_eviction_drops_oldest(small_queue):
    """同优先级超过 per_priority_limit 时，丢最老。"""
    order: list[str] = []
    block_fut = small_queue.submit(Priority.P0, _delayed_factory(order, "block", 1.0))
    time.sleep(0.05)
    # per_priority_limit=3，提交 5 个 → 淘汰头 2 个
    p2_futs = [
        small_queue.submit(Priority.P2, _delayed_factory(order, f"P2-{i}", 0.01))
        for i in range(5)
    ]

    # 等头 2 个 future 报错
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if all(p2_futs[i].done() for i in range(2)):
            break
        time.sleep(0.02)

    assert isinstance(p2_futs[0].exception(timeout=0.5), QueueEvictedError)
    assert isinstance(p2_futs[1].exception(timeout=0.5), QueueEvictedError)

    block_fut.result(timeout=5)
    for f in p2_futs[2:]:
        assert f.result(timeout=5).startswith("P2-")


def test_total_limit_keeps_only_p0():
    """总队列超 total_limit 时，按 P2→P1 顺序丢最老，直到队列长度 ≤ total_limit。"""
    q = InferenceQueue(
        per_priority_limit=20,
        total_limit=4,
        low_memory_threshold_mb=10,
        max_concurrency=1,
    )
    try:
        order: list[str] = []
        block = q.submit(Priority.P0, _delayed_factory(order, "block", 1.0))
        time.sleep(0.05)

        # 3 P0 + 3 P2 = 6，超 total=4 → 丢头 2 个 P2，剩 3 P0 + 1 P2
        p0s = [q.submit(Priority.P0, _delayed_factory(order, f"P0-{i}", 0.01)) for i in range(3)]
        p2s = [q.submit(Priority.P2, _delayed_factory(order, f"P2-{i}", 0.01)) for i in range(3)]

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if p2s[0].done() and p2s[1].done():
                break
            time.sleep(0.02)

        # 头 2 个 P2 被淘汰
        assert isinstance(p2s[0].exception(timeout=0.5), QueueEvictedError)
        assert isinstance(p2s[1].exception(timeout=0.5), QueueEvictedError)

        # block 完成后剩余 P0 + 末尾 P2 都应正常完成
        block.result(timeout=5)
        for f in p0s:
            assert f.result(timeout=5).startswith("P0-")
        assert p2s[2].result(timeout=5) == "P2-2"
    finally:
        q.shutdown()


def test_low_memory_blocks_worker():
    """可用内存低于阈值时，worker 不取任务。"""
    q = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=999_999,
        max_concurrency=1,
    )
    try:
        order: list[str] = []
        # 阈值离谱地高，所有任务都该被门禁挡住
        fut = q.submit(Priority.P0, _delayed_factory(order, "should-not-run", 0.01))
        time.sleep(0.5)
        assert not fut.done()
        assert order == []
    finally:
        q.shutdown()


def test_max_concurrency_reserves_p0_lane():
    """max_concurrency=3 时，后台最多占 2 路，P0 仍可立即获得第 3 路。"""
    q = InferenceQueue(per_priority_limit=8, total_limit=16, low_memory_threshold_mb=10, max_concurrency=3)
    try:
        order: list[str] = []
        capture = q.submit(Priority.P1, _delayed_factory(order, "capture", 0.2), lane=LANE_P1_CAPTURE)
        pre = q.submit(Priority.P1, _delayed_factory(order, "pre", 0.2), lane=LANE_P1_PREEXTRACT)
        bake = q.submit(Priority.P2, _delayed_factory(order, "bake", 0.2), lane=LANE_P2_BAKE)
        time.sleep(0.05)

        stats = q.stats()
        assert stats["running_total"] <= 2

        p0 = q.submit(Priority.P0, _delayed_factory(order, "p0", 0.01), lane=LANE_P0_QUERY)
        assert p0.result(timeout=5) == "p0"
        preempted = 0
        for f in (capture, pre, bake):
            try:
                f.result(timeout=5)
            except InferencePreemptedError:
                preempted += 1
        assert preempted >= 1
    finally:
        q.shutdown()


def test_power_aware_concurrency_uses_configured_parallelism_when_charging(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "3")
    state = {"plugged": True}

    def _power():
        return SimpleNamespace(percent=60, power_plugged=state["plugged"])

    q = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=10,
        power_provider=_power,
        global_slot_prefix=str(tmp_path / "power-slot"),
    )
    try:
        assert q.stats()["max_concurrency"] == 3
        assert q.stats()["concurrency_mode"] == "power_aware"

        state["plugged"] = False
        with q._cv:
            q._refresh_power_state_locked(force=True)

        stats = q.stats()
        assert stats["max_concurrency"] == 1
        assert stats["on_external_power"] is False
        assert stats["lane_limits"][LANE_P2_BAKE] == 1
    finally:
        q.shutdown()


def test_power_aware_slots_cap_background_across_process_queues_and_reserve_p0(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "3")
    prefix = str(tmp_path / "shared-slot")
    power = lambda: SimpleNamespace(percent=80, power_plugged=True)
    q1 = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=10,
        power_provider=power,
        global_slot_prefix=prefix,
    )
    q2 = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=10,
        power_provider=power,
        global_slot_prefix=prefix,
    )
    try:
        order: list[str] = []
        background = [
            q1.submit(Priority.P2, _delayed_factory(order, "q1-a", 0.25)),
            q1.submit(Priority.P1, _delayed_factory(order, "q1-b", 0.25)),
            q2.submit(Priority.P2, _delayed_factory(order, "q2-a", 0.25)),
            q2.submit(Priority.P1, _delayed_factory(order, "q2-b", 0.25)),
        ]
        time.sleep(0.05)

        combined_running = q1.stats()["running_total"] + q2.stats()["running_total"]
        assert combined_running <= 2

        p0 = q2.submit(
            Priority.P0,
            _delayed_factory(order, "p0", 0.01),
            lane=LANE_P0_QUERY,
        )
        assert p0.result(timeout=2) == "p0"
        preempted = 0
        for future in background:
            try:
                future.result(timeout=3)
            except InferencePreemptedError:
                preempted += 1
        assert preempted >= 1
    finally:
        q1.shutdown()
        q2.shutdown()


def test_battery_single_slot_p0_preempts_background_in_another_queue(tmp_path):
    prefix = str(tmp_path / "shared-battery-slot")
    power = lambda: SimpleNamespace(percent=60, power_plugged=False)
    q1 = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=10,
        power_provider=power,
        global_slot_prefix=prefix,
    )
    q2 = InferenceQueue(
        per_priority_limit=8,
        total_limit=16,
        low_memory_threshold_mb=10,
        power_provider=power,
        global_slot_prefix=prefix,
    )
    try:
        background_started = mock.Mock()

        def background():
            background_started()
            while True:
                raise_if_preempted()
                time.sleep(0.01)

        background_future = q1.submit(Priority.P2, background)
        deadline = time.monotonic() + 1
        while not background_started.called and time.monotonic() < deadline:
            time.sleep(0.01)

        started = time.monotonic()
        p0 = q2.submit(Priority.P0, lambda: "interactive")

        assert p0.result(timeout=1) == "interactive"
        assert isinstance(
            background_future.exception(timeout=1),
            InferencePreemptedError,
        )
        assert time.monotonic() - started < 0.2
    finally:
        q1.shutdown()
        q2.shutdown()


def test_concurrent_observers_never_create_false_interactive_demand(
    tmp_path,
    monkeypatch,
):
    """没有 P0 时，并发状态读取不能彼此误判成在线任务。"""
    monkeypatch.setattr(
        inference_queue_module,
        "_INTERACTIVE_DEMAND_LOCK_FILE",
        str(tmp_path / "interactive.lock"),
    )
    monkeypatch.setattr(
        inference_queue_module,
        "_INTERACTIVE_DEMAND_PROBE_LOCK_FILE",
        str(tmp_path / "interactive-probe.lock"),
    )
    worker_count = 8
    rounds = 200
    barrier = threading.Barrier(worker_count)

    def observe() -> int:
        false_positives = 0
        for _ in range(rounds):
            barrier.wait(timeout=2)
            false_positives += int(interactive_demand_active())
        return false_positives

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        assert sum(pool.map(lambda _: observe(), range(worker_count))) == 0
