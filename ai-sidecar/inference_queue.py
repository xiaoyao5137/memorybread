"""分级 LLM 推理任务队列。

设计目标：
- ai-sidecar 进程内所有 LLM 推理（RAG /query, /knowledge/extract, /bake/extract,
  background_processor 主循环）通过统一队列调度，避免无控制地抢 GPU/Ollama/内存。
- 优先级 P0/P1/P2：
    P0 — 用户在线咨询与创作（立即抢占后台推理）
    P1 — 时间线提炼（Timeline Extraction）
    P2 — bake 提炼大批量
- 推理并发由供电状态和模型服务实际并行度共同决定；模型并行度默认 1，
  可通过 MEMORY_BREAD_MODEL_PARALLELISM 配置，使用电池时固定为 1。
  P0 保留快速通道，P1/P2 后台 lane 不占满全部并发。
- 同优先级内 FIFO；P0 优先，跨进程后台 P1 默认最多连续执行 2 次后让位等待的
  P2（MEMORY_BREAD_BACKGROUND_PRIORITY_BURST 可配），阻塞 lane 不占就绪名额。
- 长度淘汰：
    单优先级队列 > 32：丢最老（FIFO），future.set_exception(QueueEvictedError)
    总队列 > 64：只保留 P0，P1/P2 全部 evict
- 内存门禁：可用内存 < 500MB 时 worker 暂停取任务，每 2s 重试。
- 接口：`submit_sync(priority, fn, timeout=...)` 给 Flask 同步路由用；
       内部维护 daemon 线程跑独立 asyncio event loop。
"""
from __future__ import annotations

import collections
import concurrent.futures
import enum
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import psutil

logger = logging.getLogger(__name__)


class Priority(enum.IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2


class QueueEvictedError(RuntimeError):
    """任务因队列过载被淘汰。"""


class QueueShutdownError(RuntimeError):
    """队列已关闭。"""


class QueueWaitTimeoutError(QueueEvictedError):
    """模型尚未开始执行，等待共享槽超过调度预算；可按服务繁忙重试。"""


class InferencePreemptedError(QueueEvictedError):
    """后台推理因在线咨询或创作到达而主动让出。"""


@dataclass
class _Task:
    priority: Priority
    seq: int
    fn: Callable[[], Any]
    lane: str
    future: concurrent.futures.Future = field(repr=False)
    enqueued_at: float = field(default_factory=time.monotonic)
    global_slot_handle: Any = field(default=None, repr=False)
    interactive_demand_handle: Any = field(default=None, repr=False)
    preempt_event: threading.Event = field(default_factory=threading.Event, repr=False)
    preempt_callbacks: list[Callable[[], None]] = field(default_factory=list, repr=False)
    preempt_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def request_preempt(self) -> list[Callable[[], None]]:
        with self.preempt_lock:
            if self.preempt_event.is_set():
                return []
            self.preempt_event.set()
            return list(self.preempt_callbacks)

    def register_preempt_callback(self, callback: Callable[[], None]) -> bool:
        with self.preempt_lock:
            if self.preempt_event.is_set():
                return False
            self.preempt_callbacks.append(callback)
            return True

    def unregister_preempt_callback(self, callback: Callable[[], None]) -> None:
        with self.preempt_lock:
            try:
                self.preempt_callbacks.remove(callback)
            except ValueError:
                pass


LANE_P0_QUERY = "p0_query"
LANE_P0_CREATION = "p0_creation"
LANE_P1_CAPTURE = "p1_capture"
LANE_P1_PREEXTRACT = "p1_preextract"
LANE_P2_BAKE = "p2_bake"
LANE_P2_DIARY = "p2_diary"
LANE_P2_CREATION = "p2_creation"

_DEFAULT_PER_PRIORITY_LIMIT = 32
_DEFAULT_TOTAL_LIMIT = 64
_LOW_MEMORY_THRESHOLD_MB = 500
_MEMORY_RECHECK_INTERVAL = 2.0
# 内存不足持续超过此秒数时，evict 所有 P2 任务，防止 worker 无限空转
_MEMORY_PRESSURE_EVICT_SECS = 30.0
_MAX_CONCURRENCY_CAP = 3
_DEFAULT_MAX_CONCURRENCY = 1
_POWER_STATE_REFRESH_SECS = 5.0
_BACKGROUND_PREEMPT_COOLDOWN_SECS = 120.0
_GLOBAL_SLOT_PREFIX = "/tmp/memory-bread-inference-slot"
_INTERACTIVE_DEMAND_LOCK_FILE = "/tmp/memory-bread-interactive-demand.lock"
_INTERACTIVE_DEMAND_PROBE_LOCK_FILE = (
    "/tmp/memory-bread-interactive-demand-probe.lock"
)
_RAG_LOCK_FILE = "/tmp/memory-bread-rag.lock"
_RAG_LOCK_OWNER_FILE = "/tmp/memory-bread-rag-owner.txt"
_PREEMPT_POLL_INTERVAL_SECS = 0.05
_MODEL_PARALLELISM_ENV = "MEMORY_BREAD_MODEL_PARALLELISM"
_BACKGROUND_BURST_ENV = "MEMORY_BREAD_BACKGROUND_PRIORITY_BURST"
_DEFAULT_BACKGROUND_PRIORITY_BURST = 2
_READY_LEASE_SECS = 2.0
_READY_REFRESH_SECS = 0.5


def _configured_model_parallelism() -> int:
    """返回模型服务实际可并行执行的请求数，未配置时按单路执行。"""
    try:
        configured = int(os.environ.get(_MODEL_PARALLELISM_ENV, "1"))
    except (TypeError, ValueError):
        configured = 1
    return max(1, min(_MAX_CONCURRENCY_CAP, configured))


class InferenceQueue:
    def __init__(
        self,
        per_priority_limit: int = _DEFAULT_PER_PRIORITY_LIMIT,
        total_limit: int = _DEFAULT_TOTAL_LIMIT,
        low_memory_threshold_mb: int = _LOW_MEMORY_THRESHOLD_MB,
        max_concurrency: Optional[int] = None,
        lane_limits: Optional[dict[str, int]] = None,
        power_provider: Optional[Callable[[], object]] = None,
        global_slot_prefix: Optional[str] = None,
    ):
        self._per_priority_limit = per_priority_limit
        self._total_limit = total_limit
        self._low_mem_mb = low_memory_threshold_mb
        self._power_provider = power_provider or psutil.sensors_battery
        self._power_aware = max_concurrency is None
        # 固定并发只用于内部测试；真实的供电感知队列通过 flock 在 main.py 与
        # model_api_server.py 两个进程之间共享整机并发槽。
        self._global_slot_prefix = (
            global_slot_prefix or _GLOBAL_SLOT_PREFIX
            if self._power_aware
            else None
        )
        self._last_power_state_refresh = 0.0
        self._last_background_preempted_at = 0.0
        self._on_external_power: Optional[bool] = None
        self._model_parallelism = _configured_model_parallelism()
        self._queue_identity = uuid.uuid4().hex
        try:
            self._background_priority_burst = max(
                1, min(16, int(os.environ.get(
                    _BACKGROUND_BURST_ENV, str(_DEFAULT_BACKGROUND_PRIORITY_BURST)
                )))
            )
        except (TypeError, ValueError):
            self._background_priority_burst = _DEFAULT_BACKGROUND_PRIORITY_BURST
        if self._power_aware:
            self._max_concurrency = self._power_aware_max_concurrency()
        else:
            self._max_concurrency = self._normalize_max_concurrency(max_concurrency)
        self._lane_limits = {
            LANE_P0_QUERY: _MAX_CONCURRENCY_CAP,
            LANE_P0_CREATION: _MAX_CONCURRENCY_CAP,
            LANE_P1_CAPTURE: 1,
            LANE_P1_PREEXTRACT: 1,
            LANE_P2_BAKE: self._background_concurrency_limit(),
            LANE_P2_DIARY: 1,
            LANE_P2_CREATION: self._background_concurrency_limit(),
        }
        self._creation_lane_limit_override = LANE_P2_CREATION in (lane_limits or {})
        if lane_limits:
            self._lane_limits.update({k: max(1, int(v)) for k, v in lane_limits.items()})
        self._active_total = 0
        self._active_by_lane: dict[str, int] = collections.defaultdict(int)
        self._active_by_priority: dict[Priority, int] = collections.defaultdict(int)
        self._active_tasks: dict[int, _Task] = {}
        self._queues: dict[Priority, collections.deque[_Task]] = {
            p: collections.deque() for p in Priority
        }
        self._cv = threading.Condition()
        self._seq = 0
        self._shutdown = False
        self._stats = {
            p.name: {
                "submitted": 0,
                "completed": 0,
                "evicted": 0,
                "preempted": 0,
                "timed_out": 0,
                "failed": 0,
            }
            for p in Priority
        }
        # P0 (RAG 查询) 执行时持有此文件锁，让 extractor_v2._rag_is_active() 能正确检测
        self._rag_lock_file = _RAG_LOCK_FILE
        self._rag_lock_owner_file = _RAG_LOCK_OWNER_FILE
        self._worker_threads = [
            threading.Thread(
                target=self._worker_loop,
                name=f"InferenceQueueWorker-{i + 1}",
                daemon=True,
            )
            for i in range(_MAX_CONCURRENCY_CAP)
        ]
        for worker in self._worker_threads:
            worker.start()
        logger.info(
            "InferenceQueue 启动 per_priority_limit=%d total_limit=%d low_mem_mb=%d max_concurrency=%d",
            per_priority_limit, total_limit, low_memory_threshold_mb, self._max_concurrency,
        )

    # ── 公共接口 ──────────────────────────────────────────────────────────

    def submit(
        self,
        priority: Priority,
        fn: Callable[[], Any],
        lane: Optional[str] = None,
    ) -> concurrent.futures.Future:
        """非阻塞提交：返回 Future，调用方自行 .result()。"""
        if self._shutdown:
            raise QueueShutdownError("InferenceQueue 已关闭")
        future: concurrent.futures.Future = concurrent.futures.Future()
        callbacks: list[Callable[[], None]] = []
        with self._cv:
            self._refresh_power_state_locked()
            self._seq += 1
            task = _Task(
                priority=priority,
                seq=self._seq,
                fn=fn,
                future=future,
                lane=lane or self._default_lane(priority),
            )
            if priority == Priority.P0:
                task.interactive_demand_handle = _acquire_interactive_demand(
                    global_slot_prefix=self._global_slot_prefix,
                )
                callbacks = self._request_background_preemption_locked()
            self._queues[priority].append(task)
            future.add_done_callback(
                lambda done: self._remove_cancelled_queued_task(task) if done.cancelled() else None
            )
            self._stats[priority.name]["submitted"] += 1
            self._evict_if_needed_locked()
            self._cv.notify_all()
        _invoke_preempt_callbacks(callbacks)
        return future

    def _remove_cancelled_queued_task(self, task: _Task) -> None:
        """Release a cancelled P0's demand without waiting for any active worker.

        Future.cancel() succeeds only before admission; running requests keep
        their resource handles until their transport/function has actually exited.
        """
        with self._cv:
            try:
                self._queues[task.priority].remove(task)
            except ValueError:
                return
            _release_interactive_demand(task)
            self._publish_global_readiness_locked()
            self._cv.notify_all()

    def submit_sync(
        self,
        priority: Priority,
        fn: Callable[[], Any],
        timeout: Optional[float] = None,
        lane: Optional[str] = None,
        queue_timeout: Optional[float] = None,
    ) -> Any:
        """阻塞提交；调用方超时后同步取消排队项或抢占正在执行的后台项。

        显式指定 queue_timeout 时，排队预算和 timeout 执行预算分开计算。
        未指定时保留既有从提交开始计时的语义。
        `Future.result(timeout=...)` 本身只停止等待，不会取消任务。若不显式
        中断，HTTP 已返回 504 后底层 Ollama 仍会继续占用唯一推理槽。
        """
        if getattr(_WORKER_STATE, "queue", None) is self:
            logger.debug("InferenceQueue reentrant submit_sync，直接执行 %s", priority.name)
            return fn()
        future = self.submit(priority, fn, lane=lane)
        if queue_timeout is not None:
            self._wait_for_admission(future, queue_timeout)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            self._cancel_timed_out_future(future)
            raise

    def _wait_for_admission(
        self, future: concurrent.futures.Future, queue_timeout: float,
    ) -> None:
        deadline = time.monotonic() + max(0.0, queue_timeout)
        with self._cv:
            while not future.running() and not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # admission 和取消都在同一把锁下，不能将刚开始运行的请求
                    # 当作排队超时取消，白白丢掉已经消耗的模型计算。
                    self._cancel_timed_out_future(future)
                    raise QueueWaitTimeoutError("等待模型推理槽超时，任务尚未执行")
                self._cv.wait(timeout=remaining)

    def _cancel_timed_out_future(
        self,
        future: concurrent.futures.Future,
    ) -> None:
        """取消超时 future，并触发活动任务注册的可中断 I/O 回调。"""
        callbacks: list[Callable[[], None]] = []
        timed_out_task: Optional[_Task] = None
        was_active = False
        with self._cv:
            for queue in self._queues.values():
                for index, task in enumerate(queue):
                    if task.future is not future:
                        continue
                    del queue[index]
                    _release_interactive_demand(task)
                    future.cancel()
                    timed_out_task = task
                    break
                if timed_out_task is not None:
                    break

            if timed_out_task is None:
                for task in self._active_tasks.values():
                    if task.future is future:
                        timed_out_task = task
                        was_active = True
                        callbacks = task.request_preempt()
                        break

            if timed_out_task is not None:
                self._stats[timed_out_task.priority.name]["timed_out"] += 1
                self._publish_global_readiness_locked()
                self._cv.notify_all()

        _invoke_preempt_callbacks(callbacks)
        if timed_out_task is not None:
            logger.warning(
                "InferenceQueue timeout cancel %s seq=%d active=%s",
                timed_out_task.priority.name,
                timed_out_task.seq,
                was_active,
            )

    def stats(self) -> dict[str, Any]:
        with self._cv:
            self._refresh_power_state_locked()
            return self._stats_locked()

    def is_idle(self) -> bool:
        """True when no inference task is queued or running in this process."""
        with self._cv:
            return self._active_total == 0 and all(not q for q in self._queues.values())

    def _stats_locked(self) -> dict[str, Any]:
        now = time.monotonic()
        oldest_wait_ms_by_priority = {
            p.name: (
                max(0, int((now - q[0].enqueued_at) * 1000))
                if q
                else 0
            )
            for p, q in self._queues.items()
        }
        background_retry_after_ms = (
            max(
                0,
                int(
                    (
                        _BACKGROUND_PREEMPT_COOLDOWN_SECS
                        - (now - self._last_background_preempted_at)
                    )
                    * 1000
                ),
            )
            if self._last_background_preempted_at > 0
            else 0
        )
        return {
            "queue_lengths": {p.name: len(q) for p, q in self._queues.items()},
            "queue_lengths_by_lane": self._queue_lengths_by_lane_locked(),
            "oldest_wait_ms_by_priority": oldest_wait_ms_by_priority,
            "oldest_wait_ms": max(oldest_wait_ms_by_priority.values(), default=0),
            "running_total": self._active_total,
            "running_by_lane": dict(self._active_by_lane),
            "running_by_priority": {
                priority.name: self._active_by_priority.get(priority, 0)
                for priority in Priority
            },
            "max_concurrency": self._max_concurrency,
            "concurrency_mode": "power_aware" if self._power_aware else "fixed",
            "on_external_power": self._on_external_power,
            "cross_process_limit": self._max_concurrency if self._global_slot_prefix else None,
            "model_parallelism": self._model_parallelism,
            "background_priority_burst": self._background_priority_burst,
            "interactive_demand_active": interactive_demand_active(),
            "background_retry_after_ms": background_retry_after_ms,
            "lane_limits": dict(self._lane_limits),
            "totals": dict(self._stats),
            "available_mb": self._available_mb(),
        }

    def shutdown(self) -> None:
        with self._cv:
            self._shutdown = True
            self._cv.notify_all()
            for q in self._queues.values():
                while q:
                    task = q.popleft()
                    _release_interactive_demand(task)
                    if not task.future.done():
                        task.future.set_exception(QueueShutdownError("队列已关闭"))
            self._publish_global_readiness_locked()

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _evict_if_needed_locked(self) -> None:
        # 1) 同优先级队列超 limit：FIFO 丢最老
        for p, q in self._queues.items():
            while len(q) > self._per_priority_limit:
                victim = q.popleft()
                _release_interactive_demand(victim)
                self._stats[p.name]["evicted"] += 1
                if not victim.future.done():
                    victim.future.set_exception(
                        QueueEvictedError(
                            f"{p.name} 队列超 {self._per_priority_limit}，最老任务被淘汰"
                        )
                    )
                logger.warning(
                    "InferenceQueue evict %s seq=%d 等待时长=%.2fs",
                    p.name, victim.seq, time.monotonic() - victim.enqueued_at,
                )

        # 2) 总队列超 total_limit：保留 P0，丢 P1/P2 最老
        total = sum(len(q) for q in self._queues.values())
        while total > self._total_limit:
            evicted = False
            for p in (Priority.P2, Priority.P1):
                if self._queues[p]:
                    victim = self._queues[p].popleft()
                    _release_interactive_demand(victim)
                    self._stats[p.name]["evicted"] += 1
                    if not victim.future.done():
                        victim.future.set_exception(
                            QueueEvictedError(
                                f"总队列超 {self._total_limit}，{p.name} 任务被让位 P0"
                            )
                        )
                    logger.warning(
                        "InferenceQueue overflow evict %s seq=%d total_was=%d",
                        p.name, victim.seq, total,
                    )
                    total -= 1
                    evicted = True
                    break
            if not evicted:
                break  # 全是 P0，无法再淘汰

    def _pop_highest_locked(self) -> Optional[_Task]:
        if self._global_slot_prefix:
            try:
                return self._pop_with_global_fairness_locked()
            except (ImportError, IOError, OSError) as exc:
                logger.warning("跨进程优先级协调不可用，保留共享槽限制: %s", exc)
        return self._pop_local_locked()

    def _pop_local_locked(
        self, background_priority: Optional[Priority] = None,
    ) -> Optional[_Task]:
        for p in Priority:  # IntEnum 自然顺序 P0 < P1 < P2
            if p != Priority.P0 and background_priority is not None and p != background_priority:
                continue
            q = self._queues[p]
            for idx, task in enumerate(q):
                if self._can_run_locked(task) and self._try_acquire_global_slot_locked(task):
                    del q[idx]
                    if not task.future.set_running_or_notify_cancel():
                        self._release_global_slot(task)
                        _release_interactive_demand(task)
                        return None
                    self._active_total += 1
                    self._active_by_lane[task.lane] += 1
                    self._active_by_priority[task.priority] += 1
                    self._active_tasks[task.seq] = task
                    self._cv.notify_all()
                    return task
        return None

    def _ready_background_priorities_locked(self) -> list[int]:
        if self._shutdown or self._available_mb() < self._low_mem_mb:
            return []
        return [
            int(priority) for priority in (Priority.P1, Priority.P2)
            if any(not task.future.cancelled() and self._can_run_locked(task)
                   for task in self._queues[priority])
        ]

    def _publish_global_readiness_locked(self) -> None:
        if self._global_slot_prefix:
            try:
                self._pop_with_global_fairness_locked(admit=False)
            except Exception:
                # 就绪登记是调度提示，失败不能中断任务/释放槽的生命周期。
                logger.warning("更新跨进程推理就绪状态失败", exc_info=True)

    def _pop_with_global_fairness_locked(self, *, admit: bool = True) -> Optional[_Task]:
        """跨进程按就绪优先级分配槽，连续 P1 有界，P2 也能持续前进。

        槽锁本身没有优先级，释放槽的进程往往会连续抢回槽。用另一把短锁
        将“检查就绪需求、选择优先级、获取槽”串行化。仅登记可运行的后台
        任务；登记有短租期，进程退出或暂停后不会阻塞另一个进程。
        本方法不抢占正在运行的任务，P0 始终绕过后台交替规则。
        """
        import fcntl

        task: Optional[_Task] = None
        try:
            with open(f"{self._global_slot_prefix}-scheduler.json", "a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                try:
                    state = json.load(handle)
                    if not isinstance(state, dict):
                        state = {}
                except (ValueError, TypeError):
                    state = {}
                original = json.dumps(state, sort_keys=True)
                # JSON 租约由不同进程比较；Python 3.9/macOS 的 monotonic
                # 起点并不共享，必须使用公共时基。本地排队/执行预算仍用
                # monotonic。回拨后的未来租约和前跳后的过期租约在下面
                # 一并失效，publish_local 会按当前时间重新登记本进程。
                now = time.time()
                peers = state.get("ready")
                if not isinstance(peers, dict):
                    peers = {}
                peers = {
                    key: entry for key, entry in peers.items()
                    if isinstance(entry, dict)
                    and isinstance(entry.get("updated"), (int, float))
                    and 0 <= now - entry["updated"] < _READY_LEASE_SECS
                    and isinstance(entry.get("priorities"), list)
                }

                def publish_local() -> None:
                    ready = self._ready_background_priorities_locked()
                    previous = peers.get(self._queue_identity, {})
                    if not ready:
                        peers.pop(self._queue_identity, None)
                    elif (previous.get("priorities") != ready
                          or now - previous.get("updated", 0) >= _READY_REFRESH_SECS):
                        peers[self._queue_identity] = {"priorities": ready, "updated": now}

                publish_local()
                ready_priorities = {
                    value for entry in peers.values()
                    for value in entry["priorities"] if value in (1, 2)
                }
                preferred = Priority.P1
                streak = state.get("p1_streak", 0)
                if not isinstance(streak, int):
                    streak = 0
                if 2 in ready_priorities and (
                    1 not in ready_priorities or streak >= self._background_priority_burst
                ):
                    preferred = Priority.P2
                task = (
                    self._pop_local_locked(background_priority=preferred) if admit else None
                )
                if task is not None and task.priority != Priority.P0:
                    state["p1_streak"] = (
                        min(self._background_priority_burst, streak + 1)
                        if task.priority == Priority.P1 else 0
                    )
                    publish_local()
                state["ready"] = peers
                encoded = json.dumps(state, sort_keys=True)
                if encoded != original:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(encoded)
                    handle.flush()
                return task
        except (IOError, OSError):
            if task is None:
                raise
            # 已取得槽的任务必须交给 worker；状态文件写入/关闭失败不能丢失它。
            logger.warning("推理任务已取得槽，协调状态写入失败，继续执行", exc_info=True)
            return task

    def _request_background_preemption_locked(self) -> list[Callable[[], None]]:
        callbacks: list[Callable[[], None]] = []
        for task in self._active_tasks.values():
            if task.priority == Priority.P0:
                continue
            callbacks.extend(task.request_preempt())
            logger.info(
                "InferenceQueue preempt request %s seq=%d for interactive P0",
                task.priority.name,
                task.seq,
            )
        return callbacks

    def _available_mb(self) -> int:
        try:
            return int(psutil.virtual_memory().available / 1024 / 1024)
        except Exception:
            return 1 << 30  # 拿不到就当作"内存充足"，fail-open

    def _worker_loop(self) -> None:
        _low_mem_since: Optional[float] = None
        while True:
            task: Optional[_Task] = None
            with self._cv:
                # 等待非空 / 关闭
                while not self._shutdown and all(
                    not q for q in self._queues.values()
                ):
                    self._cv.wait()
                if self._shutdown:
                    return
                # 内存门禁：内存不足时不取任务，2s 后再检查
                avail = self._available_mb()
                if avail < self._low_mem_mb:
                    if _low_mem_since is None:
                        _low_mem_since = time.monotonic()
                    logger.warning(
                        "InferenceQueue 内存门禁 avail=%dMB < %dMB，暂停 worker %.1fs",
                        avail, self._low_mem_mb, _MEMORY_RECHECK_INTERVAL,
                    )
                    # 内存持续不足超过阈值时，evict 所有 P2 任务，防止 worker 无限空转
                    if time.monotonic() - _low_mem_since >= _MEMORY_PRESSURE_EVICT_SECS:
                        q2 = self._queues[Priority.P2]
                        while q2:
                            victim = q2.popleft()
                            self._stats[Priority.P2.name]["evicted"] += 1
                            if not victim.future.done():
                                victim.future.set_exception(
                                    QueueEvictedError(
                                        f"内存持续不足 {_MEMORY_PRESSURE_EVICT_SECS:.0f}s，P2 任务被强制淘汰"
                                    )
                                )
                        logger.error(
                            "InferenceQueue 内存压力超 %.0fs，P2 全部 evict",
                            _MEMORY_PRESSURE_EVICT_SECS,
                        )
                        self._cv.notify_all()
                        _low_mem_since = None  # 重置计时，下一轮压力重新计
                    self._cv.wait(timeout=_MEMORY_RECHECK_INTERVAL)
                    continue
                _low_mem_since = None  # 内存恢复正常，重置计时
                self._refresh_power_state_locked()
                task = self._pop_highest_locked()
                if task is None:
                    self._cv.wait(timeout=0.1)
                    continue

            if task is None:
                continue

            preempt_watch_stop = threading.Event()
            preempt_watcher = None
            if task.priority != Priority.P0:
                preempt_watcher = threading.Thread(
                    target=self._watch_external_preemption,
                    args=(task, preempt_watch_stop),
                    name=f"InferencePreemptWatcher-{task.seq}",
                    daemon=True,
                )
                preempt_watcher.start()

            wait_ms = int((time.monotonic() - task.enqueued_at) * 1000)
            logger.info(
                "InferenceQueue exec %s seq=%d wait_ms=%d",
                task.priority.name, task.seq, wait_ms,
            )

            # P0 (RAG 查询) 执行时持有 RAG 文件锁
            # 这样 extractor_v2._rag_is_active() 能正确检测到 RAG 正在占用 Ollama
            rag_lock_fd = None
            if task.priority == Priority.P0:
                try:
                    import fcntl
                    rag_lock_fd = open(self._rag_lock_file, "w")
                    fcntl.flock(rag_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        with open(self._rag_lock_owner_file, "w") as f:
                            f.write("query")
                    except Exception:
                        pass
                    logger.debug("InferenceQueue P0 获取 RAG 锁成功")
                except (IOError, OSError):
                    # 拿不到锁说明 RAG 已被其他进程持有，继续执行（队列已保证串行）
                    logger.debug("InferenceQueue P0 RAG 锁已被占用，继续执行")
                    if rag_lock_fd:
                        rag_lock_fd.close()
                        rag_lock_fd = None

            t0 = time.monotonic()
            try:
                _WORKER_STATE.queue = self
                _WORKER_STATE.task = task
                raise_if_preempted()
                result = task.fn()
                raise_if_preempted()
                if not task.future.done():
                    task.future.set_result(result)
                with self._cv:
                    self._stats[task.priority.name]["completed"] += 1
            except InferencePreemptedError as exc:
                logger.info(
                    "InferenceQueue task 已让出 %s seq=%d",
                    task.priority.name,
                    task.seq,
                )
                if not task.future.done():
                    task.future.set_exception(exc)
                with self._cv:
                    self._stats[task.priority.name]["preempted"] += 1
                    self._last_background_preempted_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "InferenceQueue task 失败 %s seq=%d",
                    task.priority.name, task.seq,
                )
                if not task.future.done():
                    task.future.set_exception(exc)
                with self._cv:
                    self._stats[task.priority.name]["failed"] += 1
            finally:
                _WORKER_STATE.queue = None
                _WORKER_STATE.task = None
                preempt_watch_stop.set()
                exec_ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "InferenceQueue done %s seq=%d exec_ms=%d",
                    task.priority.name, task.seq, exec_ms,
                )
                # 释放 RAG 锁
                if rag_lock_fd is not None:
                    try:
                        import fcntl
                        fcntl.flock(rag_lock_fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                    finally:
                        rag_lock_fd.close()
                with self._cv:
                    self._active_total = max(0, self._active_total - 1)
                    self._active_tasks.pop(task.seq, None)
                    if self._active_by_lane.get(task.lane, 0) <= 1:
                        self._active_by_lane.pop(task.lane, None)
                    else:
                        self._active_by_lane[task.lane] -= 1
                    if self._active_by_priority.get(task.priority, 0) <= 1:
                        self._active_by_priority.pop(task.priority, None)
                    else:
                        self._active_by_priority[task.priority] -= 1
                    # 先公布当前 lane 已就绪，再释放真实槽。否则另一进程
                    # 会在本进程下一次轮询前连取多个低优先级任务。
                    self._publish_global_readiness_locked()
                    self._release_global_slot(task)
                    _release_interactive_demand(task)
                    self._cv.notify_all()

    @staticmethod
    def _watch_external_preemption(task: _Task, stop_event: threading.Event) -> None:
        while not stop_event.wait(_PREEMPT_POLL_INTERVAL_SECS):
            if not interactive_demand_active():
                continue
            callbacks = task.request_preempt()
            if callbacks:
                logger.info(
                    "InferenceQueue cross-process preempt %s seq=%d",
                    task.priority.name,
                    task.seq,
                )
                _invoke_preempt_callbacks(callbacks)
            return

    @staticmethod
    def _normalize_max_concurrency(value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = _DEFAULT_MAX_CONCURRENCY
        return max(1, min(_MAX_CONCURRENCY_CAP, parsed))

    @staticmethod
    def _default_lane(priority: Priority) -> str:
        if priority == Priority.P0:
            return LANE_P0_QUERY
        if priority == Priority.P1:
            return LANE_P1_CAPTURE
        return LANE_P2_BAKE

    def _can_run_locked(self, task: _Task) -> bool:
        if self._active_total >= self._max_concurrency:
            return False
        lane_limit = self._lane_limits.get(task.lane, 1)
        if self._active_by_lane.get(task.lane, 0) >= lane_limit:
            return False
        if task.priority != Priority.P0:
            background_limit = (
                self._max_concurrency
                if self._max_concurrency <= 1
                else self._max_concurrency - 1
            )
            if self._active_total >= background_limit:
                return False
        return True

    def _background_concurrency_limit(self) -> int:
        if self._max_concurrency <= 1:
            return 1
        return self._max_concurrency - 1

    def _try_acquire_global_slot_locked(self, task: _Task) -> bool:
        """跨进程获取整机推理槽；多并发时 P1/P2 为 P0 保留最后一个槽。

        保留槽只允许 P0 使用，从而即使 main.py 与 model_api_server.py
        同时有后台积压，也不会把在线咨询完全堵住。
        """
        # 正常跨进程准入已持 scheduler 短锁，P0 的 demand 注册使用同一把锁。
        # 必须在取槽前拦住后台，而不能让它先占槽再被 watcher 取消。
        # 此检查不放在 _can_run_locked：后台仍须公布资源/lane 就绪状态，
        # P0 完成后才能立即恢复原有 P1/P2 公平调度。
        if task.priority != Priority.P0 and interactive_demand_active():
            return False
        if not self._global_slot_prefix:
            return True
        if task.global_slot_handle is not None:
            return True

        try:
            import fcntl
        except ImportError:
            # 非 Unix 平台无法使用 flock；保留进程内限制。
            return True

        slot_count = (
            self._max_concurrency
            if task.priority == Priority.P0 or self._max_concurrency <= 1
            else self._max_concurrency - 1
        )
        for slot_index in range(slot_count):
            path = f"{self._global_slot_prefix}-{slot_index}.lock"
            handle = None
            try:
                handle = open(path, "a+")
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                task.global_slot_handle = handle
                return True
            except (IOError, OSError):
                if handle is not None:
                    handle.close()
        return False

    @staticmethod
    def _release_global_slot(task: _Task) -> None:
        handle = task.global_slot_handle
        if handle is None:
            return
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
        except (ImportError, IOError, OSError):
            pass
        finally:
            handle.close()
            task.global_slot_handle = None

    def _power_aware_max_concurrency(self) -> int:
        """外接电源用 3 路；电池或传感器异常时用 1 路。

        无电池设备（例如台式机）会返回 None，按外接电源处理。
        读取异常选择 1 路，避免移动设备在状态未知时意外拉高功耗。
        """
        try:
            battery = self._power_provider()
        except Exception as exc:
            logger.warning("读取推理队列供电状态失败，降级为单并发: %s", exc)
            self._on_external_power = False
            return 1
        if battery is None:
            self._on_external_power = True
            return self._model_parallelism
        self._on_external_power = bool(getattr(battery, "power_plugged", False))
        return self._model_parallelism if self._on_external_power else 1

    def _refresh_power_state_locked(self, *, force: bool = False) -> None:
        if not self._power_aware:
            return
        now = time.monotonic()
        if not force and now - self._last_power_state_refresh < _POWER_STATE_REFRESH_SECS:
            return
        self._last_power_state_refresh = now
        next_concurrency = self._power_aware_max_concurrency()
        if next_concurrency == self._max_concurrency:
            return
        previous = self._max_concurrency
        self._max_concurrency = next_concurrency
        self._lane_limits[LANE_P2_BAKE] = self._background_concurrency_limit()
        if not self._creation_lane_limit_override:
            self._lane_limits[LANE_P2_CREATION] = self._background_concurrency_limit()
        logger.info(
            "InferenceQueue 供电状态切换 max_concurrency=%d->%d plugged=%s",
            previous,
            next_concurrency,
            self._on_external_power,
        )
        self._cv.notify_all()

    def _queue_lengths_by_lane_locked(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for q in self._queues.values():
            for task in q:
                result[task.lane] = result.get(task.lane, 0) + 1
        return result


def _acquire_interactive_demand(global_slot_prefix: Optional[str] = None):
    """P0 从提交到完成持有共享锁，跨进程通知后台推理立即让出。"""
    if global_slot_prefix:
        try:
            import fcntl
            # 与后台的 demand 检查 + 取槽构成同一个准入临界区，避免 P0
            # 正好注册在后台检查之后、获取空槽之前的竞态。
            with open(f"{global_slot_prefix}-scheduler.json", "a+") as scheduler:
                fcntl.flock(scheduler, fcntl.LOCK_EX)
                return _acquire_interactive_demand()
        except (ImportError, IOError, OSError):
            logger.warning("交互推理准入协调不可用，保留需求锁和运行中抢占")
    try:
        import fcntl
        handle = open(_INTERACTIVE_DEMAND_LOCK_FILE, "a+")
        fcntl.flock(handle, fcntl.LOCK_SH)
        return handle
    except (ImportError, IOError, OSError) as exc:
        logger.warning("获取在线任务抢占锁失败，降级为进程内抢占: %s", exc)
        return None


def _release_interactive_demand(task: _Task) -> None:
    handle = task.interactive_demand_handle
    if handle is None:
        return
    try:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)
    except (ImportError, IOError, OSError):
        pass
    finally:
        handle.close()
        task.interactive_demand_handle = None


def interactive_demand_active() -> bool:
    """检测其他进程是否有已提交或正在运行的咨询/创作 P0。

    多个观察者若同时对 P0 共享锁做排他探测，会彼此冲突并误报 P0 活跃。
    因此先用独立文件锁串行化探测，再检查真正的 demand 锁。
    """
    try:
        import fcntl
        probe_handle = open(_INTERACTIVE_DEMAND_PROBE_LOCK_FILE, "a+")
        try:
            fcntl.flock(probe_handle, fcntl.LOCK_EX)
            demand_handle = open(_INTERACTIVE_DEMAND_LOCK_FILE, "a+")
            try:
                fcntl.flock(demand_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(demand_handle, fcntl.LOCK_UN)
                return False
            except (IOError, OSError):
                return True
            finally:
                demand_handle.close()
        finally:
            try:
                fcntl.flock(probe_handle, fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
            probe_handle.close()
    except (ImportError, IOError, OSError):
        return False


def _invoke_preempt_callbacks(callbacks: list[Callable[[], None]]) -> None:
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            logger.debug("执行推理抢占回调失败: %s", exc)


def current_task_preempt_requested() -> bool:
    task = getattr(_WORKER_STATE, "task", None)
    if task is None or task.priority == Priority.P0:
        return False
    if not task.preempt_event.is_set() and interactive_demand_active():
        _invoke_preempt_callbacks(task.request_preempt())
    return task.preempt_event.is_set()


def raise_if_preempted() -> None:
    if current_task_preempt_requested():
        raise InferencePreemptedError("后台推理已让出在线咨询或创作任务")


def register_current_preempt_callback(
    callback: Callable[[], None],
) -> Callable[[], None]:
    """注册当前后台任务的中断回调；P0 或非队列线程中为空操作。"""
    task = getattr(_WORKER_STATE, "task", None)
    if task is None or task.priority == Priority.P0:
        return lambda: None
    if not task.register_preempt_callback(callback):
        _invoke_preempt_callbacks([callback])
        return lambda: None
    return lambda: task.unregister_preempt_callback(callback)


# ── 模块级单例（model_api_server.py 启动时引用）──────────────────────────────

_GLOBAL: Optional[InferenceQueue] = None
_GLOBAL_LOCK = threading.Lock()
_WORKER_STATE = threading.local()


def get_global_queue() -> InferenceQueue:
    """获取进程级供电感知单例队列，惰性创建。"""
    global _GLOBAL
    if _GLOBAL is None:
        with _GLOBAL_LOCK:
            if _GLOBAL is None:
                _GLOBAL = InferenceQueue()
    return _GLOBAL
