"""MemoryBread 后台任务的统一节能策略。

节能模式默认开启，并把后台提炼分成三档：
- charging：外接电源，保持最大吞吐；
- battery：使用电池且电量高于阈值，降低扫描频率、批量和 bake 并发；
- critical_battery：使用电池且电量不高于阈值，暂停后台提炼。

没有电池信息的设备按外接电源处理，避免台式机或系统 API 不可用时误停任务。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

import psutil

logger = logging.getLogger(__name__)

ENERGY_SAVING_MODE_KEY = "performance.energy_saving_mode"
LOW_BATTERY_THRESHOLD_PERCENT = 20.0

BATTERY_TIMELINE_INTERVAL_SECS = 120
BATTERY_TIMELINE_BATCH_SIZE = 4
BATTERY_BAKE_INTERVAL_SECS = 30 * 60
BATTERY_BAKE_LIMIT = 1
BATTERY_BAKE_CONCURRENCY = 1

# 充电时每 30 秒检查一次 bake backlog；已有 run 在执行时 core 会拒绝重复 run，
# 完成后下一次检查会立即续上，避免大批积压每轮额外空等 5 分钟。
CHARGING_BAKE_INTERVAL_SECS = 30
# 32K 长文档单候选可能包含 bundle + merge 两次推理。缩小调度切片不会降低
# 单路模型吞吐，但能让每个 run 稳定落在 30 分钟总预算内，避免处理已有进度
# 后被整批误标 failed；完成后 30 秒内会自动续下一批。
CHARGING_BAKE_LIMIT = 10
# 并发 3 用于流水线化候选间的 HTTP 往返与存储写入，已是 core 侧 clamp 上限 1~3；
# 本地模型推理槽位仍由 inference_queue 的全局上限约束，不会真正放大算力开销。
CHARGING_BAKE_CONCURRENCY = 3
MODEL_PARALLELISM_ENV = "MEMORY_BREAD_MODEL_PARALLELISM"
MAX_MODEL_PARALLELISM = 3

CRITICAL_BATTERY_RECHECK_SECS = 5 * 60


@dataclass(frozen=True)
class EnergyProfile:
    mode: str
    saving_enabled: bool
    on_external_power: bool
    battery_percent: Optional[float]
    allow_background_extraction: bool
    allow_diary: bool
    timeline_interval_secs: int
    timeline_batch_size: int
    bake_interval_secs: int
    bake_limit: int
    bake_concurrency: int


class EnergyPolicy:
    def __init__(
        self,
        db_path: str,
        *,
        battery_provider: Optional[Callable[[], object]] = None,
        low_battery_threshold: float = LOW_BATTERY_THRESHOLD_PERCENT,
        model_parallelism: Optional[int] = None,
    ) -> None:
        self.db_path = db_path
        self.battery_provider = battery_provider or psutil.sensors_battery
        self.low_battery_threshold = float(low_battery_threshold)
        self.model_parallelism = self._normalize_model_parallelism(model_parallelism)

    def is_energy_saving_enabled(self) -> bool:
        """读取持久化开关；缺失或读取失败时按默认开启处理。"""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM user_preferences WHERE key = ? LIMIT 1",
                    (ENERGY_SAVING_MODE_KEY,),
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            logger.debug("读取节能模式偏好失败，使用默认开启: %s", exc)
            return True

        if row is None:
            return True
        return str(row[0]).strip().lower() not in {"0", "false", "no", "off"}

    def current_profile(
        self,
        *,
        base_timeline_interval_secs: int = 30,
        base_timeline_batch_size: int = 20,
    ) -> EnergyProfile:
        saving_enabled = self.is_energy_saving_enabled()
        battery_percent, on_external_power = self._read_battery_state()

        if not saving_enabled:
            # 用户显式关闭节能时不区分电源态，统一用配置的模型并发度
            automatic_concurrency = self.model_parallelism
            return EnergyProfile(
                mode="unrestricted",
                saving_enabled=False,
                on_external_power=on_external_power,
                battery_percent=battery_percent,
                allow_background_extraction=True,
                allow_diary=True,
                timeline_interval_secs=max(1, int(base_timeline_interval_secs)),
                timeline_batch_size=max(1, int(base_timeline_batch_size)),
                bake_interval_secs=CHARGING_BAKE_INTERVAL_SECS,
                bake_limit=CHARGING_BAKE_LIMIT,
                bake_concurrency=automatic_concurrency,
            )

        if on_external_power:
            return EnergyProfile(
                mode="charging",
                saving_enabled=True,
                on_external_power=True,
                battery_percent=battery_percent,
                allow_background_extraction=True,
                allow_diary=True,
                timeline_interval_secs=max(1, int(base_timeline_interval_secs)),
                timeline_batch_size=max(1, int(base_timeline_batch_size)),
                bake_interval_secs=CHARGING_BAKE_INTERVAL_SECS,
                bake_limit=CHARGING_BAKE_LIMIT,
                bake_concurrency=self.model_parallelism,
            )

        if battery_percent is not None and battery_percent <= self.low_battery_threshold:
            return EnergyProfile(
                mode="critical_battery",
                saving_enabled=True,
                on_external_power=False,
                battery_percent=battery_percent,
                allow_background_extraction=False,
                allow_diary=False,
                timeline_interval_secs=CRITICAL_BATTERY_RECHECK_SECS,
                timeline_batch_size=0,
                bake_interval_secs=0,
                bake_limit=0,
                bake_concurrency=0,
            )

        return EnergyProfile(
            mode="battery",
            saving_enabled=True,
            on_external_power=False,
            battery_percent=battery_percent,
            allow_background_extraction=True,
            allow_diary=False,
            timeline_interval_secs=max(
                BATTERY_TIMELINE_INTERVAL_SECS,
                int(base_timeline_interval_secs),
            ),
            timeline_batch_size=min(
                BATTERY_TIMELINE_BATCH_SIZE,
                max(1, int(base_timeline_batch_size)),
            ),
            bake_interval_secs=BATTERY_BAKE_INTERVAL_SECS,
            bake_limit=BATTERY_BAKE_LIMIT,
            bake_concurrency=BATTERY_BAKE_CONCURRENCY,
        )

    def _read_battery_state(self) -> tuple[Optional[float], bool]:
        try:
            battery = self.battery_provider()
        except Exception as exc:
            logger.debug("读取电池状态失败，按外接电源处理: %s", exc)
            return None, True

        if battery is None:
            return None, True

        raw_percent = getattr(battery, "percent", None)
        try:
            percent = float(raw_percent) if raw_percent is not None else None
        except (TypeError, ValueError):
            percent = None
        return percent, bool(getattr(battery, "power_plugged", False))

    @staticmethod
    def _normalize_model_parallelism(configured: Optional[int]) -> int:
        raw_value = (
            configured
            if configured is not None
            else os.environ.get(MODEL_PARALLELISM_ENV, CHARGING_BAKE_CONCURRENCY)
        )
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = CHARGING_BAKE_CONCURRENCY
        return max(1, min(MAX_MODEL_PARALLELISM, parsed))
