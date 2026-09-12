"""inference_queue 的功能测试。"""
import threading
import time
import json
import multiprocessing
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
    LANE_P2_CREATION,
    Priority,
    QueueEvictedError,
    QueueWaitTimeoutError,
    interactive_demand_active,
    raise_if_preempted,
)


@pytest.fixture(autouse=True)
def isolated_inference_locks(tmp_path, monkeypatch):
    """队列回归不得通过 /tmp 交互锁抢占正在运行的真实后台推理。"""
    for name, filename in (
        ("_GLOBAL_SLOT_PREFIX", "inference-slot"),
        ("_INTERACTIVE_DEMAND_LOCK_FILE", "interactive.lock"),
        ("_INTERACTIVE_DEMAND_PROBE_LOCK_FILE", "interactive-probe.lock"),
        ("_RAG_LOCK_FILE", "rag.lock"),
        ("_RAG_LOCK_OWNER_FILE", "rag-owner.txt"),
    ):
        monkeypatch.setattr(inference_queue_module, name, str(tmp_path / filename))


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


def test_cancelled_queued_p0_immediately_releases_demand(small_queue):
    """The running task need not complete for cancelled queued demand to disappear."""
    started = threading.Event()
    release = threading.Event()
    called = mock.Mock()

    def foreground():
        started.set()
        release.wait(2)

    # Use P2 so its own demand does not obscure the cancelled P0 handle.
    background = small_queue.submit(Priority.P2, foreground)
    try:
        assert started.wait(1)
        waiting = small_queue.submit(Priority.P0, called)
        assert interactive_demand_active()
        assert waiting.cancel()
        assert not interactive_demand_active()
        assert small_queue.stats()["queue_lengths"]["P0"] == 0
        called.assert_not_called()
    finally:
        release.set()
        with pytest.raises(InferencePreemptedError):
            background.result(timeout=1)


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


@pytest.mark.parametrize("priority", [Priority.P1, Priority.P2])
def test_independent_queue_budget_preserves_full_execution_budget(small_queue, priority):
    started = threading.Event()

    def occupying():
        started.set()
        time.sleep(0.18)

    blocker = small_queue.submit(Priority.P2, occupying)
    assert started.wait(1)
    # 0.18s 排队 + 0.18s 执行超过原来的总 timeout，但实际执行仍在预算内。
    assert small_queue.submit_sync(
        priority, _delayed_factory([], "completed", 0.18),
        queue_timeout=1, timeout=0.3,
    ) == "completed"
    blocker.result(timeout=1)
    assert small_queue.stats()["totals"][priority.name]["timed_out"] == 0


def test_queue_budget_expiry_never_runs_or_preempts_candidate(small_queue):
    release = threading.Event()
    started = threading.Event()
    attempted = mock.Mock()

    def occupying():
        started.set()
        release.wait(2)

    blocker = small_queue.submit(Priority.P2, occupying)
    assert started.wait(1)
    try:
        with pytest.raises(QueueWaitTimeoutError):
            small_queue.submit_sync(
                Priority.P2, attempted, queue_timeout=0.04, timeout=1,
            )
        assert small_queue.stats()["queue_lengths"]["P2"] == 0
        assert small_queue.stats()["totals"]["P2"]["preempted"] == 0
    finally:
        release.set()
    blocker.result(timeout=1)
    assert small_queue.submit_sync(Priority.P2, lambda: "next", timeout=1) == "next"
    attempted.assert_not_called()


def test_separate_execution_budget_still_cancels_active_overrun(small_queue):
    started = threading.Event()

    def background():
        started.set()
        while True:
            raise_if_preempted()
            time.sleep(0.005)

    with pytest.raises(FutureTimeoutError):
        small_queue.submit_sync(
            Priority.P2, background, queue_timeout=1, timeout=0.04,
        )
    assert started.is_set()
    assert small_queue.submit_sync(Priority.P2, lambda: "next", timeout=1) == "next"
    assert small_queue.stats()["totals"]["P2"]["preempted"] == 1


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
        assert q.stats()["lane_limits"][LANE_P2_CREATION] == 2

        state["plugged"] = False
        with q._cv:
            q._refresh_power_state_locked(force=True)

        stats = q.stats()
        assert stats["max_concurrency"] == 1
        assert stats["on_external_power"] is False
        assert stats["lane_limits"][LANE_P2_BAKE] == 1
        assert stats["lane_limits"][LANE_P2_CREATION] == 1
        state["plugged"] = True
        with q._cv:
            q._refresh_power_state_locked(force=True)
        assert q.stats()["lane_limits"][LANE_P2_CREATION] == 2
    finally:
        q.shutdown()


def test_independent_creation_prefetches_use_model_capacity_and_leave_a_p0_slot(monkeypatch):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "3")
    queue = InferenceQueue(low_memory_threshold_mb=0, power_provider=lambda: None)
    started = [threading.Event() for _ in range(3)]
    release = threading.Event()

    def background(index):
        started[index].set()
        assert release.wait(5)
        return index

    futures = [queue.submit(Priority.P2, lambda index=index: background(index), lane=LANE_P2_CREATION)
               for index in range(3)]
    try:
        assert started[0].wait(2) and started[1].wait(2)
        assert not started[2].is_set(), "background work must reserve the third model slot for P0"
        assert queue.stats()["running_by_lane"][LANE_P2_CREATION] == 2
        futures[2].cancel()
        foreground = queue.submit(Priority.P0, lambda: not release.is_set(), lane=LANE_P0_QUERY)
        assert foreground.result(timeout=2) is True, "P0 must run while both independent callbacks hold their slots"
    finally:
        release.set()
        for future in futures[:2]:
            try:
                future.result(timeout=3)
            except InferencePreemptedError:
                pass
        queue.shutdown()


def test_creation_prefetches_stay_serial_for_a_single_slot_model(monkeypatch):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    queue = InferenceQueue(low_memory_threshold_mb=0, power_provider=lambda: None)
    first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()

    def first():
        first_started.set()
        assert release.wait(5)
        return "first"

    first_future = queue.submit(Priority.P2, first, lane=LANE_P2_CREATION)
    second_future = queue.submit(Priority.P2, lambda: second_started.set(), lane=LANE_P2_CREATION)
    try:
        assert first_started.wait(2)
        assert not second_started.wait(0.1)
        assert queue.stats()["lane_limits"][LANE_P2_CREATION] == 1
        release.set()
        assert first_future.result(timeout=2) == "first"
        second_future.result(timeout=2)
        assert second_started.is_set()
    finally:
        release.set()
        queue.shutdown()


def test_explicit_creation_lane_limit_survives_power_changes(monkeypatch):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "3")
    power = {"plugged": False}
    queue = InferenceQueue(low_memory_threshold_mb=0, lane_limits={LANE_P2_CREATION: 1},
        power_provider=lambda: SimpleNamespace(power_plugged=power["plugged"]))
    try:
        power["plugged"] = True
        with queue._cv:
            queue._refresh_power_state_locked(force=True)
        assert queue.stats()["max_concurrency"] == 3
        assert queue.stats()["lane_limits"][LANE_P2_CREATION] == 1
    finally:
        queue.shutdown()


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


def test_cross_process_slot_prioritizes_p1_and_bounds_p2_wait(tmp_path, monkeypatch):
    """独立调度器共用文件槽时，释放槽的一方不能连续抢回 P2。"""
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    monkeypatch.setenv("MEMORY_BREAD_BACKGROUND_PRIORITY_BURST", "2")
    prefix = str(tmp_path / "fair-slot")
    power = lambda: SimpleNamespace(percent=80, power_plugged=True)
    q1 = InferenceQueue(power_provider=power, global_slot_prefix=prefix)
    q2 = InferenceQueue(power_provider=power, global_slot_prefix=prefix)
    release = threading.Event()
    started = threading.Event()
    order = []

    def occupying():
        started.set()
        release.wait(3)
        return "initial"

    try:
        blocker = q2.submit(Priority.P2, occupying)
        assert started.wait(1)
        p1s = [q1.submit(Priority.P1, _delayed_factory(order, "P1", 0.02))
               for _ in range(6)]
        p2s = [q2.submit(Priority.P2, _delayed_factory(order, "P2", 0.02))
               for _ in range(3)]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with q1._cv:
                q1._pop_with_global_fairness_locked(admit=False)
            state = json.loads((tmp_path / "fair-slot-scheduler.json").read_text())
            if any(1 in entry["priorities"] for entry in state["ready"].values()):
                break
        else:
            pytest.fail("P1 ready demand was not published")
        release.set()
        blocker.result(timeout=1)
        for future in p1s + p2s:
            future.result(timeout=4)
        assert order[0] == "P1"
        assert order.index("P2") <= 2
        assert order == ["P1", "P1", "P2"] * 3
        assert q1.stats()["totals"]["P1"]["preempted"] == 0
        assert q2.stats()["totals"]["P2"]["preempted"] == 0
    finally:
        release.set()
        q1.shutdown()
        q2.shutdown()


@pytest.mark.parametrize("peer_age", [10, -10])
def test_expired_or_future_peer_readiness_cannot_block_background_slot(
    tmp_path, monkeypatch, peer_age,
):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    prefix = str(tmp_path / "expired-slot")
    (tmp_path / "expired-slot-scheduler.json").write_text(json.dumps({
        "ready": {"exited-process": {
            "priorities": [1],
            "updated": time.time() - peer_age,
        }}, "p1_streak": 0,
    }))
    q = InferenceQueue(
        power_provider=lambda: None, global_slot_prefix=prefix,
    )
    try:
        assert q.submit_sync(Priority.P2, lambda: "resumed", timeout=1) == "resumed"
    finally:
        q.shutdown()


def test_wall_clock_rollback_republishes_local_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    clock = SimpleNamespace(time=lambda: 100.0, monotonic=time.monotonic)
    monkeypatch.setattr(inference_queue_module, "time", clock)
    prefix = str(tmp_path / "rollback-slot")
    q = InferenceQueue(power_provider=lambda: None, global_slot_prefix=prefix)
    monkeypatch.setattr(q, "_available_mb", lambda: 1_000_000)
    try:
        # 持本地 CV 阻止 worker 取任务，直接检查同一就绪任务在回拨后重登记。
        with q._cv:
            pending = q.submit(Priority.P1, lambda: "resumed")
            q._publish_global_readiness_locked()
            state_path = tmp_path / "rollback-slot-scheduler.json"
            first = json.loads(state_path.read_text())
            assert first["ready"][q._queue_identity]["updated"] == 100.0
            clock.time = lambda: 90.0
            q._publish_global_readiness_locked()
            refreshed = json.loads(state_path.read_text())
            assert refreshed["ready"][q._queue_identity] == {
                "priorities": [1], "updated": 90.0,
            }
        assert pending.result(timeout=1) == "resumed"
    finally:
        q.shutdown()


def test_low_memory_peer_does_not_advertise_unrunnable_p1(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    prefix = str(tmp_path / "memory-slot")
    q1 = InferenceQueue(
        power_provider=lambda: None, global_slot_prefix=prefix,
        low_memory_threshold_mb=999_999,
    )
    q2 = InferenceQueue(power_provider=lambda: None, global_slot_prefix=prefix)
    try:
        pending = q1.submit(Priority.P1, lambda: "blocked")
        assert q2.submit_sync(Priority.P2, lambda: "progress", timeout=1) == "progress"
        assert not pending.done()
    finally:
        q1.shutdown()
        q2.shutdown()


@pytest.mark.parametrize("failed_operation", ["write", "flush"])
def test_scheduler_write_failure_cannot_lose_admitted_task(tmp_path, monkeypatch, failed_operation):
    import builtins

    real_open = builtins.open

    class _BrokenStateFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def __getattr__(self, name):
            if name == failed_operation:
                def fail(*_args):
                    raise OSError("simulated state write failure")
                return fail
            return getattr(self.handle, name)

    def state_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if str(path).endswith("-scheduler.json"):
            return _BrokenStateFile(handle)
        return handle

    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    monkeypatch.setattr(inference_queue_module, "open", state_open, raising=False)
    q = InferenceQueue(
        power_provider=lambda: None,
        global_slot_prefix=str(tmp_path / "io-failure-slot"),
    )
    try:
        assert q.submit_sync(Priority.P2, lambda: "first", timeout=1) == "first"
        assert q.submit_sync(Priority.P2, lambda: "second", timeout=1) == "second"
        deadline = time.monotonic() + 1
        while not q.is_idle() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert q.stats()["running_total"] == 0
    finally:
        q.shutdown()


def _run_fairness_peer(prefix, priority, count, ready, output, sequence, clock_offset):
    """spawn 子进程也显式隔离锁，不能继承真实 /tmp 在线需求。"""
    import os

    os.environ["MEMORY_BREAD_MODEL_PARALLELISM"] = "1"
    os.environ["MEMORY_BREAD_BACKGROUND_PRIORITY_BURST"] = "2"
    inference_queue_module._INTERACTIVE_DEMAND_LOCK_FILE = prefix + "-interactive.lock"
    inference_queue_module._INTERACTIVE_DEMAND_PROBE_LOCK_FILE = prefix + "-probe.lock"
    # Python 3.9/macOS 的 monotonic 起点随进程不同；显式放大差异覆盖错时基。
    inference_queue_module.time = SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock_offset,
        time=time.time,
    )
    q = InferenceQueue(power_provider=lambda: None, global_slot_prefix=prefix)
    q._available_mb = lambda: 1_000_000

    def run():
        # 用跨进程序号记录真实开始顺序，不比较各 Python 进程的 monotonic。
        with sequence.get_lock():
            sequence.value += 1
            output.put((sequence.value, priority))
        time.sleep(0.02)

    try:
        futures = [q.submit(Priority(priority), run) for _ in range(count)]
        with q._cv:
            q._publish_global_readiness_locked()
        ready.set()
        for future in futures:
            future.result(timeout=10)
    finally:
        q.shutdown()


@pytest.mark.parametrize("clock_offset", [0.0, 30.0])
def test_two_os_processes_share_bounded_background_priority(tmp_path, clock_offset):
    import fcntl

    prefix = str(tmp_path / "process-slot")
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    sequence = ctx.Value("i", 0)
    ready = [ctx.Event(), ctx.Event()]
    processes = [ctx.Process(
        target=_run_fairness_peer,
        args=(prefix, priority, count, ready[index], output, sequence,
              clock_offset if priority == 2 else 0.0),
    ) for index, (priority, count) in enumerate(((1, 6), (2, 3)))]
    with open(prefix + "-0.lock", "a+") as slot:
        fcntl.flock(slot, fcntl.LOCK_EX)
        try:
            for process in processes:
                process.start()
            assert all(event.wait(5) for event in ready)
            with open(prefix + "-scheduler.json") as scheduler:
                fcntl.flock(scheduler, fcntl.LOCK_SH)
                state = json.load(scheduler)
            assert {
                priority for peer in state["ready"].values()
                for priority in peer["priorities"]
            } == {1, 2}
            fcntl.flock(slot, fcntl.LOCK_UN)
            observed = [output.get(timeout=10) for _ in range(9)]
            assert [priority for _, priority in sorted(observed)] == [1, 1, 2] * 3
            for process in processes:
                process.join(timeout=5)
                assert process.exitcode == 0
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            output.close()


def _run_interactive_admission_peer(prefix, registered, admit, started, finish):
    """Keep P0 queued with an empty slot to expose background admission races."""
    inference_queue_module._INTERACTIVE_DEMAND_LOCK_FILE = prefix + "-interactive.lock"
    inference_queue_module._INTERACTIVE_DEMAND_PROBE_LOCK_FILE = prefix + "-probe.lock"
    inference_queue_module._RAG_LOCK_FILE = prefix + "-rag.lock"
    inference_queue_module._RAG_LOCK_OWNER_FILE = prefix + "-rag-owner.txt"
    queue = InferenceQueue(power_provider=lambda: None, global_slot_prefix=prefix)
    original_can_run = queue._can_run_locked
    queue._can_run_locked = lambda task: admit.is_set() and original_can_run(task)

    def foreground():
        started.set()
        assert finish.wait(10)

    try:
        future = queue.submit(Priority.P0, foreground)
        registered.set()
        future.result(timeout=15)
    finally:
        queue.shutdown()


def _run_background_admission_peer(prefix, ready, inspect, started, output):
    inference_queue_module._INTERACTIVE_DEMAND_LOCK_FILE = prefix + "-interactive.lock"
    inference_queue_module._INTERACTIVE_DEMAND_PROBE_LOCK_FILE = prefix + "-probe.lock"
    queue = InferenceQueue(power_provider=lambda: None, global_slot_prefix=prefix)

    def background(priority):
        started.set()
        return priority

    try:
        futures = [queue.submit(priority, lambda p=priority: background(p.name))
                   for priority in (Priority.P1, Priority.P2)]
        with queue._cv:
            queue._publish_global_readiness_locked()
        ready.set()
        assert inspect.wait(10)
        output.put([(future.running(), future.done()) for future in futures])
        results = [future.result(timeout=10) for future in futures]
        output.put((results, queue.stats()["totals"]))
    finally:
        queue.shutdown()


def test_cross_process_pending_p0_blocks_background_admission_until_completion(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setenv("MEMORY_BREAD_MODEL_PARALLELISM", "1")
    prefix = str(tmp_path / "interactive-admission")
    context = multiprocessing.get_context("spawn")
    registered, admit, foreground_started, finish = [context.Event() for _ in range(4)]
    ready, inspect, background_started = [context.Event() for _ in range(3)]
    output = context.Queue()
    foreground = context.Process(target=_run_interactive_admission_peer, args=(
        prefix, registered, admit, foreground_started, finish,
    ))
    background = context.Process(target=_run_background_admission_peer, args=(
        prefix, ready, inspect, background_started, output,
    ))
    try:
        foreground.start()
        assert registered.wait(5)
        background.start()
        assert ready.wait(5)
        assert not background_started.wait(0.25)
        inspect.set()
        # It must stay queued, not start, abort before fn(), and consume a retry.
        assert output.get(timeout=2) == [(False, False), (False, False)]
        with open(prefix + "-scheduler.json") as scheduler:
            fcntl.flock(scheduler, fcntl.LOCK_SH)
            state = json.load(scheduler)
        assert {priority for entry in state["ready"].values()
                for priority in entry["priorities"]} == {1, 2}

        admit.set()
        assert foreground_started.wait(2)
        assert not background_started.wait(0.15)
        finish.set()
        results, stats = output.get(timeout=3)
        assert results == ["P1", "P2"]
        assert stats["P1"]["preempted"] == stats["P2"]["preempted"] == 0
        assert stats["P1"]["completed"] == stats["P2"]["completed"] == 1
        for process in (foreground, background):
            process.join(timeout=3)
            assert process.exitcode == 0
    finally:
        admit.set()
        finish.set()
        inspect.set()
        for process in (foreground, background):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(timeout=2)
        output.close()


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
