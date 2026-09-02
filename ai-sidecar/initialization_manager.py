"""MemoryBread 首次启动一键初始化编排器。

该模块只向客户端暴露品牌化组件和稳定阶段 ID。供应商模型名只存在于
sidecar 内部，不进入客户端状态或云端诊断包。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psutil

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "initialization.v1"
MANAGED_OLLAMA_VERSION = "0.30.8"
MANAGED_OLLAMA_URL = (
    "https://github.com/ollama/ollama/releases/download/"
    f"v{MANAGED_OLLAMA_VERSION}/ollama-darwin.tgz"
)
# 境内访问 GitHub Release 不稳定，提供加速代理作为候选源。
# 所有候选源下载后都必须通过同一个 SHA256 校验，镜像内容无法被篡改。
# 可通过 MEMORY_BREAD_OLLAMA_DOWNLOAD_MIRRORS 追加自建镜像（逗号分隔，优先级最高）。
MANAGED_OLLAMA_MIRROR_PREFIXES = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)
MANAGED_OLLAMA_SHA256 = "52acbca4e89c53db9abc586a22b5633fd101db293177264b9a0fe5d64a42a064"
NORMAL_OLLAMA_PORT = 11434
SANDBOX_OLLAMA_PORT = 11435
SANDBOX_CORE_PORT = 17070
OLLAMA_GUI_APP_ROOT = Path("/Applications/Ollama.app").resolve()
MIN_FREE_DISK_GB = 6.0
# 引擎健康检查失败后的降级宽限秒数。应用启动早期本地 AI 引擎可能还未
# 被拉起，立即把完成状态降级为中断会让用户永久卡在恢复页。
INVALID_STATE_GRACE_SECONDS = 90.0
MAX_PROCESS_LOG_BYTES = 5 * 1024 * 1024
PROCESS_LOG_BACKUPS = 2
CORE_STARTUP_GRACE_SECONDS = 10.0
CORE_REPAIR_TIMEOUT_SECONDS = 45.0
AUTO_REPAIRABLE_STAGE_ERRORS = frozenset(
    {
        "RUNTIME_DOWNLOAD_FAILED",
        "RUNTIME_CHECKSUM_MISMATCH",
        "RUNTIME_START_FAILED",
        "MODEL_DOWNLOAD_FAILED",
        "SKILLS_TOOLS_INITIALIZATION_FAILED",
        "QUALITY_GATE_FAILED",
        "FEATURE_SMOKE_TEST_FAILED",
    }
)
SANDBOX_COLD_INSTALL_STAGES = frozenset(
    {
        "inference_engine",
        "capture_model",
        "vector_model",
        "database",
        "skills_tools",
    }
)

# 这些供应商模型名不得出现在 get_status() 或 get_report_bundle() 中。
_CAPTURE_MODEL_NAME = "qwen3.5:4b"
_VECTOR_MODEL_NAME = "qllama/bge-small-zh-v1.5:q4_k_m"

STAGES = (
    ("preflight", "检查运行环境", 0, 8),
    ("inference_engine", "准备本地 AI 引擎", 8, 25),
    ("capture_model", "准备采集提炼能力", 25, 52),
    ("vector_model", "准备语义检索能力", 52, 68),
    ("database", "准备本地记忆库", 68, 78),
    ("skills_tools", "准备技能与工具", 78, 86),
    ("quality_gate", "执行完整质检", 86, 93),
    ("feature_smoke_tests", "验证核心功能", 93, 100),
)

ERROR_SUGGESTIONS = {
    "UNSUPPORTED_PLATFORM": "当前版本暂不支持此操作系统，请升级到受支持的 macOS。",
    "UNSUPPORTED_ARCHITECTURE": "当前处理器架构暂不受支持。",
    "INSUFFICIENT_DISK_SPACE": "请释放至少 6 GB 可用空间后重试。",
    "RUNTIME_DOWNLOAD_FAILED": "请检查网络连接后重试（已依次尝试境内加速源与官方源），已经完成的内容不会重复下载。",
    "RUNTIME_CHECKSUM_MISMATCH": "下载文件校验失败，请重试；应用不会执行未通过校验的文件。",
    "RUNTIME_START_FAILED": "本地 AI 引擎未能启动，请重试或上报诊断。",
    "MODEL_DOWNLOAD_FAILED": "模型下载未完成，请检查网络和磁盘空间后重试。",
    "DATABASE_INITIALIZATION_FAILED": "本地记忆库未能完成初始化，请重试或上报诊断。",
    "CORE_SERVICE_UNAVAILABLE": "本地核心服务已自动重启但仍未就绪，请重新启动应用；若仍失败再上报诊断。",
    "CORE_PORT_CONFLICT": "本地服务端口被其他程序占用，关闭占用 7070 端口的程序后重试。",
    "SKILLS_TOOLS_INITIALIZATION_FAILED": "内置技能或工具未能加载，请重新安装最新版应用。",
    "QUALITY_GATE_FAILED": "组件质检未通过，请重试；重复项会自动跳过。",
    "FEATURE_SMOKE_TEST_FAILED": "核心功能测试未通过，请重试或上报诊断。",
    "INITIALIZATION_COMPONENT_MISSING": "检测到本地能力不完整，点击恢复即可自动复用仍然可用的内容。",
    "SANDBOX_CORE_UNAVAILABLE": "未找到可用于隔离测试的本地核心服务，请先完成开发构建。",
    "SANDBOX_ISOLATION_FAILED": "隔离环境检测到正式组件或遗留进程，请关闭测试模式后重新开启。",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InitializationFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class InitializationManager:
    """持久化、可判重的一键初始化任务。"""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = (base_dir or (Path.home() / ".memory-bread")).resolve()
        self.normal_state_path = self.base_dir / "initialization" / "state.json"
        self.mode_path = self.base_dir / "initialization" / "test-mode.json"
        self.sandbox_root = (self.base_dir / "initialization-sandbox").resolve()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._processes: dict[str, subprocess.Popen] = {}
        self._invalid_since: dict[str, Optional[float]] = {}
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._mark_interrupted_runs()

    # ── public lifecycle ──────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            state = self._refresh_completed_state(self._load_active_state())
            state["test_mode_enabled"] = self._test_mode_enabled()
            return self._public_state(state)

    def generate_local_nickname(self) -> str:
        """Use the installed local model once to create an installation nickname."""
        with self._lock:
            state = self._refresh_completed_state(self._load_state("normal"))
            if (
                state.get("state") != "completed"
                or not state.get("quality_gate", {}).get("passed")
                or self._test_mode_enabled()
            ):
                raise InitializationFailure(
                    "LOCAL_MODEL_NOT_READY",
                    "本地模型尚未完成初始化",
                )

        prompt = (
            "请给一位刚完成记忆面包初始化的用户起一个中文昵称。"
            "昵称必须有性格或状态形容词，并包含一种面包或烘焙食品，"
            "例如“倔强的牛角面包”。只输出昵称，不要引号、解释、标点或换行，"
            "总长度不超过十二个汉字。随机灵感编号："
            + uuid.uuid4().hex[-8:]
        )
        generated = self._ollama_generate("normal", prompt)
        first_line = generated.strip().splitlines()[0] if generated.strip() else ""
        nickname = re.sub(r"^昵称\s*[:：]?\s*", "", first_line)
        nickname = re.sub(r"[\s\"'“”‘’。，、！？!?.:：;；]", "", nickname)
        bread_words = (
            "面包", "吐司", "法棍", "可颂", "牛角包", "贝果", "餐包",
            "碱水结", "司康", "甜甜圈", "椒盐卷饼",
        )
        if 2 <= len(nickname) <= 12 and any(word in nickname for word in bread_words):
            return nickname

        fallbacks = (
            "倔强的牛角面包",
            "慢烤的酸种面包",
            "好奇的小法棍",
            "踏实的吐司片",
            "发光的碱水结",
            "温柔的奶香餐包",
        )
        return fallbacks[uuid.uuid4().int % len(fallbacks)]

    def start(self, mode: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            active_mode = "sandbox" if self._test_mode_enabled() else "normal"
            requested_mode = mode or active_mode
            if requested_mode not in {"normal", "sandbox"}:
                raise InitializationFailure("INVALID_INITIALIZATION_MODE", "初始化模式不受支持")
            if requested_mode != active_mode:
                raise InitializationFailure(
                    "INITIALIZATION_MODE_MISMATCH",
                    "请先在调试面板切换初始化测试模式",
                )

            state = self._refresh_completed_state(self._load_state(requested_mode))
            if state.get("state") == "running" and self._thread and self._thread.is_alive():
                return self._public_state(state)
            if state.get("state") == "completed" and state.get("quality_gate", {}).get("passed"):
                return self._public_state(state)

            state = self._new_state(requested_mode)
            if requested_mode == "sandbox":
                cold_start = self._sandbox_artifacts_absent()
                state["sandbox_cold_start"] = cold_start
                state["sandbox_isolation"] = self._sandbox_isolation_status(cold_start)
            self._save_state(state)
            logger.info(
                "initialization_started run_id=%s mode=%s",
                state["run_id"],
                requested_mode,
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(requested_mode, state["run_id"]),
                daemon=True,
                name=f"initialization-{requested_mode}",
            )
            self._thread.start()
            return self._public_state(state)

    def enable_test_mode(self, confirmation: str) -> dict[str, Any]:
        if confirmation != "ENABLE_INITIALIZATION_TEST_MODE":
            raise InitializationFailure("CONFIRMATION_REQUIRED", "需要二次确认后才能开启初始化测试模式")
        with self._lock:
            current = self._load_active_state()
            if current.get("state") == "running":
                raise InitializationFailure("INITIALIZATION_ALREADY_RUNNING", "初始化正在运行，暂时不能切换模式")
            if self._test_mode_enabled():
                return self.get_status()
            # 必须先停止上一次测试遗留的进程，再删除沙箱目录。反过来会丢失
            # PID/进程身份标记，让旧 11435 服务继续伪装成全新的冷启动环境。
            self._stop_sandbox_processes()
            self._safe_remove_sandbox()
            self.sandbox_root.mkdir(parents=True, exist_ok=True)
            ollama_port, core_port = self._allocate_sandbox_ports()
            self._write_json_atomic(
                self.mode_path,
                {
                    "enabled": True,
                    "enabled_at": _utc_now(),
                    "sandbox_id": str(uuid.uuid4()),
                    "ollama_port": ollama_port,
                    "core_port": core_port,
                },
            )
            sandbox_state = self._new_state("sandbox")
            sandbox_state["state"] = "not_started"
            sandbox_state["run_id"] = None
            sandbox_state["sandbox_cold_start"] = True
            sandbox_state["sandbox_isolation"] = self._sandbox_isolation_status(True)
            self._save_state(sandbox_state)
            return self.get_status()

    def disable_test_mode(self, confirmation: str) -> dict[str, Any]:
        if confirmation != "DISABLE_INITIALIZATION_TEST_MODE":
            raise InitializationFailure("CONFIRMATION_REQUIRED", "需要确认后才能关闭初始化测试模式")
        with self._lock:
            state = self._load_active_state()
            if state.get("state") == "running":
                raise InitializationFailure("INITIALIZATION_ALREADY_RUNNING", "请等待当前初始化结束后再关闭测试模式")
            self._stop_sandbox_processes()
            self._safe_remove_sandbox()
            self.mode_path.unlink(missing_ok=True)
            return self.get_status()

    def get_report_bundle(self) -> dict[str, Any]:
        """生成严格白名单诊断包，不包含日志正文、路径、主机名或用户内容。"""
        with self._lock:
            state = self._load_active_state()
            installation_id = self._installation_id()
            environment = state.get("environment") or self._environment_snapshot()
            stages = state.get("stages", [])
            checks = []
            for stage in stages:
                checks.append(
                    {
                        "id": stage["id"],
                        "status": stage["status"],
                        "duration_ms": stage.get("duration_ms"),
                        "error_code": stage.get("error_code"),
                    }
                )
            for check in state.get("quality_gate", {}).get("checks", []):
                checks.append(
                    {
                        "id": f"quality.{check['id']}",
                        "status": check["status"],
                        "duration_ms": check.get("duration_ms"),
                        "error_code": check.get("error_code"),
                    }
                )
            for check in state.get("smoke_tests", []):
                checks.append(
                    {
                        "id": f"smoke.{check['id']}",
                        "status": check["status"],
                        "duration_ms": check.get("duration_ms"),
                        "error_code": check.get("error_code"),
                    }
                )
            components = [
                self._component_report(stages, "inference_engine", "local_inference_engine"),
                self._component_report(stages, "capture_model", "capture_extraction_model"),
                self._component_report(stages, "vector_model", "vector_model"),
                self._component_report(stages, "database", "local_database"),
                self._component_report(stages, "skills_tools", "skills_tools"),
            ]
            return {
                "schema_version": SCHEMA_VERSION,
                "run_id": state.get("run_id"),
                "installation_id": installation_id,
                "client_version": os.environ.get("MEMORY_BREAD_CLIENT_VERSION", "unknown"),
                "occurred_at": state.get("finished_at") or _utc_now(),
                "failed_stage": state.get("current_stage") if state.get("state") == "failed" else None,
                "error_code": state.get("error_code"),
                "summary": self._safe_summary(state.get("message")),
                "environment": {
                    "os": environment.get("os"),
                    "os_version": environment.get("os_version"),
                    "architecture": environment.get("architecture"),
                    "memory_gb": environment.get("memory_gb"),
                    "disk_free_gb": environment.get("disk_free_gb"),
                    "mode": state.get("mode"),
                },
                "components": components,
                "checks": checks,
            }

    # ── state machine ─────────────────────────────────────────────────────

    def _run(self, mode: str, run_id: str) -> None:
        try:
            handlers: dict[str, Callable[[str, dict[str, Any]], tuple[bool, str]]] = {
                "preflight": self._stage_preflight,
                "inference_engine": self._stage_inference_engine,
                "capture_model": self._stage_capture_model,
                "vector_model": self._stage_vector_model,
                "database": self._stage_database,
                "skills_tools": self._stage_skills_tools,
                "quality_gate": self._stage_quality_gate,
                "feature_smoke_tests": self._stage_feature_smoke_tests,
            }
            for stage_id, _label, start_progress, end_progress in STAGES:
                state = self._load_state(mode)
                if state.get("run_id") != run_id:
                    return
                self._begin_stage(state, stage_id, start_progress)
                started = time.monotonic()
                try:
                    skipped, detail = self._run_stage_with_auto_repair(
                        mode,
                        stage_id,
                        handlers[stage_id],
                        state,
                    )
                except InitializationFailure:
                    raise
                except Exception as exc:
                    logger.exception("initialization stage failed stage=%s", stage_id)
                    raise InitializationFailure(self._default_error_code(stage_id), str(exc)) from exc
                if (
                    mode == "sandbox"
                    and bool(state.get("sandbox_cold_start"))
                    and stage_id in SANDBOX_COLD_INSTALL_STAGES
                    and skipped
                ):
                    raise InitializationFailure(
                        "SANDBOX_ISOLATION_FAILED",
                        f"隔离冷启动阶段不应复用已有组件: {stage_id}",
                    )
                state = self._load_state(mode)
                self._finish_stage(
                    state,
                    stage_id,
                    "skipped" if skipped else "succeeded",
                    end_progress,
                    detail,
                    int((time.monotonic() - started) * 1000),
                )

            state = self._load_state(mode)
            state.update(
                {
                    "state": "completed",
                    "progress": 100,
                    "current_stage": "feature_smoke_tests",
                    "message": "初始化与质检已完成",
                    "error_code": None,
                    "can_retry": False,
                    "can_report": False,
                    "finished_at": _utc_now(),
                }
            )
            state["quality_gate"]["passed"] = True
            self._save_state(state)
            logger.info(
                "initialization_finished run_id=%s mode=%s result=completed",
                run_id,
                mode,
            )
            if mode == "normal":
                self._notify_os_completion()
        except InitializationFailure as exc:
            with self._lock:
                state = self._load_state(mode)
                stage_id = state.get("current_stage")
                public_message = self._redact_internal_terms(str(exc))
                for stage in state["stages"]:
                    if stage["id"] == stage_id:
                        stage.update(
                            {
                                "status": "failed",
                                "error_code": exc.code,
                                "detail": public_message,
                                "finished_at": _utc_now(),
                            }
                        )
                state.update(
                    {
                        "state": "failed",
                        "message": public_message,
                        "error_code": exc.code,
                        "suggestion": ERROR_SUGGESTIONS.get(exc.code, "请重试或上报诊断。"),
                        "can_retry": True,
                        "can_report": True,
                        "finished_at": _utc_now(),
                    }
                )
                self._save_state(state)
                logger.warning(
                    "initialization_finished run_id=%s mode=%s result=failed stage_id=%s error_code=%s",
                    run_id,
                    mode,
                    stage_id,
                    exc.code,
                )

    def _run_stage_with_auto_repair(
        self,
        mode: str,
        stage_id: str,
        handler: Callable[[str, dict[str, Any]], tuple[bool, str]],
        state: dict[str, Any],
    ) -> tuple[bool, str]:
        recovery_started = False
        for attempt in range(2):
            state = self._load_state(mode)
            try:
                result = handler(mode, state)
                if recovery_started:
                    latest = self._load_state(mode)
                    self._set_recovery_state(
                        latest,
                        status="succeeded",
                        action=self._auto_repair_action(stage_id),
                        attempt=1,
                        max_attempts=1,
                        error_code=None,
                        message="自动修复已完成，继续初始化",
                    )
                return result
            except Exception as error:
                exc = error if isinstance(error, InitializationFailure) else InitializationFailure(
                    self._default_error_code(stage_id),
                    str(error),
                )
                if (
                    attempt > 0
                    or exc.code not in AUTO_REPAIRABLE_STAGE_ERRORS
                    or not self._prepare_stage_auto_repair(mode, stage_id, exc)
                ):
                    if recovery_started:
                        latest = self._load_state(mode)
                        self._set_recovery_state(
                            latest,
                            status="exhausted",
                            action=self._auto_repair_action(stage_id),
                            attempt=1,
                            max_attempts=1,
                            error_code=exc.code,
                            message="自动修复未能解决问题",
                        )
                    if isinstance(error, InitializationFailure):
                        raise
                    raise exc from error
                recovery_started = True
                latest = self._load_state(mode)
                self._set_recovery_state(
                    latest,
                    status="running",
                    action=self._auto_repair_action(stage_id),
                    attempt=1,
                    max_attempts=1,
                    error_code=exc.code,
                    message="检测到可恢复问题，正在自动修复",
                )
                logger.warning(
                    "initialization_auto_repair stage_id=%s error_code=%s action=%s",
                    stage_id,
                    exc.code,
                    self._auto_repair_action(stage_id),
                )
        raise AssertionError("unreachable auto repair loop")

    def _prepare_stage_auto_repair(
        self,
        mode: str,
        stage_id: str,
        failure: InitializationFailure,
    ) -> bool:
        if failure.code == "RUNTIME_START_FAILED":
            return self._stop_owned_ollama_for_repair(mode) or not self._port_in_use(
                self._ollama_port(mode)
            )
        if stage_id == "quality_gate":
            try:
                state = self._load_state(mode)
                self._stage_inference_engine(mode, state)
                self._stage_capture_model(mode, state)
                self._stage_vector_model(mode, state)
                self._stage_database(mode, state)
            except InitializationFailure:
                logger.warning("quality gate prerequisite auto repair did not complete")
                return False
        return True

    @staticmethod
    def _auto_repair_action(stage_id: str) -> str:
        return {
            "inference_engine": "repair_local_ai_engine",
            "capture_model": "resume_capture_model",
            "vector_model": "resume_vector_model",
            "database": "restart_core_service",
            "skills_tools": "rebuild_skills_tools_manifest",
            "quality_gate": "recheck_prerequisites",
            "feature_smoke_tests": "retry_failed_probe",
        }.get(stage_id, "retry_stage")

    def _set_recovery_state(
        self,
        state: dict[str, Any],
        *,
        status: str,
        action: str,
        attempt: int,
        max_attempts: int,
        error_code: Optional[str],
        message: str,
    ) -> None:
        recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
        same_recovery = recovery.get("action") == action and recovery.get("status") in {
            "waiting",
            "running",
        }
        started_at = recovery.get("started_at") if same_recovery else _utc_now()
        state["recovery"] = {
            "status": status,
            "action": action,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_code": error_code,
            "started_at": started_at,
            "finished_at": _utc_now() if status in {"succeeded", "exhausted"} else None,
        }
        state["message"] = message
        for stage in state.get("stages", []):
            if stage.get("id") == state.get("current_stage"):
                stage["detail"] = message
        self._save_state(state)

    def _begin_stage(self, state: dict[str, Any], stage_id: str, progress: int) -> None:
        with self._lock:
            state["state"] = "running"
            state["current_stage"] = stage_id
            state["progress"] = max(int(state.get("progress", 0)), progress)
            state["message"] = next(label for key, label, _, _ in STAGES if key == stage_id)
            for stage in state["stages"]:
                if stage["id"] == stage_id:
                    stage.update(
                        {
                            "status": "running",
                            "progress": 0,
                            "detail": state["message"],
                            "error_code": None,
                            "started_at": _utc_now(),
                        }
                    )
            self._save_state(state)

    def _finish_stage(
        self,
        state: dict[str, Any],
        stage_id: str,
        status: str,
        progress: int,
        detail: str,
        duration_ms: int,
    ) -> None:
        with self._lock:
            state["progress"] = max(int(state.get("progress", 0)), progress)
            state["message"] = detail
            for stage in state["stages"]:
                if stage["id"] == stage_id:
                    stage.update(
                        {
                            "status": status,
                            "progress": 100,
                            "detail": detail,
                            "duration_ms": duration_ms,
                            "finished_at": _utc_now(),
                        }
                    )
            self._save_state(state)
            logger.info(
                "initialization_stage_finished run_id=%s stage_id=%s result=%s duration_ms=%s",
                state.get("run_id"),
                stage_id,
                status,
                duration_ms,
            )

    # ── stages ────────────────────────────────────────────────────────────

    def _stage_preflight(self, mode: str, state: dict[str, Any]) -> tuple[bool, str]:
        environment = self._environment_snapshot()
        system = environment["os"]
        architecture = environment["architecture"]
        allow_unsupported = os.environ.get("MEMORY_BREAD_INITIALIZATION_ALLOW_UNSUPPORTED") == "1"
        if system != "darwin" and not allow_unsupported:
            raise InitializationFailure("UNSUPPORTED_PLATFORM", "当前系统暂不支持自动初始化")
        if architecture not in {"arm64", "aarch64", "x86_64", "amd64"} and not allow_unsupported:
            raise InitializationFailure("UNSUPPORTED_ARCHITECTURE", "当前处理器架构暂不受支持")
        if float(environment["disk_free_gb"]) < MIN_FREE_DISK_GB:
            raise InitializationFailure("INSUFFICIENT_DISK_SPACE", "可用磁盘空间不足 6 GB")
        state["environment"] = environment
        self._save_state(state)
        return False, "运行环境满足初始化要求"

    def _stage_inference_engine(self, mode: str, state: dict[str, Any]) -> tuple[bool, str]:
        base_url = self._ollama_base_url(mode)
        if self._ollama_healthy(base_url):
            if mode == "sandbox" and not self._sandbox_process_owned("ollama"):
                raise InitializationFailure(
                    "SANDBOX_ISOLATION_FAILED",
                    "隔离端口被非本次沙箱的本地 AI 服务占用",
                )
            if mode == "normal" and self._ollama_gui_running():
                managed_running = self._managed_ollama_process_owned(mode)
                executable = self._managed_ollama_executable(mode)
                if executable is None:
                    executable = self._install_managed_ollama(mode)
                self._stop_ollama_gui()
                if managed_running and self._ollama_healthy(base_url):
                    state["legacy_runtime_migrated"] = True
                    self._save_state(state)
                    return False, "本地 AI 引擎已切换为无界面后台运行"
                deadline = time.monotonic() + 10
                while self._port_in_use(self._ollama_port(mode)) and time.monotonic() < deadline:
                    time.sleep(0.25)
                if self._port_in_use(self._ollama_port(mode)):
                    raise InitializationFailure(
                        "RUNTIME_START_FAILED",
                        "旧本地 AI 引擎未能退出，请关闭后重试",
                    )
                self._start_ollama(mode, executable)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if self._ollama_healthy(base_url):
                        state["legacy_runtime_migrated"] = True
                        self._save_state(state)
                        return False, "本地 AI 引擎已切换为无界面后台运行"
                    time.sleep(0.5)
                raise InitializationFailure("RUNTIME_START_FAILED", "本地 AI 引擎启动超时")
            state["runtime_reused"] = True
            self._save_state(state)
            detail = (
                "隔离 AI 引擎已由本次测试启动，继续使用"
                if mode == "sandbox"
                else "本地 AI 引擎已就绪，已跳过安装"
            )
            return True, detail

        executable = self._managed_ollama_executable(mode)
        if executable is None:
            executable = self._install_managed_ollama(mode)
        self._start_ollama(mode, executable)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._ollama_healthy(base_url):
                return False, "本地 AI 引擎已安装并启动"
            time.sleep(0.5)
        raise InitializationFailure("RUNTIME_START_FAILED", "本地 AI 引擎启动超时")

    def _stage_capture_model(self, mode: str, _state: dict[str, Any]) -> tuple[bool, str]:
        return self._ensure_model(mode, _CAPTURE_MODEL_NAME, "采集提炼模型")

    def _stage_vector_model(self, mode: str, _state: dict[str, Any]) -> tuple[bool, str]:
        return self._ensure_model(mode, _VECTOR_MODEL_NAME, "向量模型")

    def _stage_database(self, mode: str, _state: dict[str, Any]) -> tuple[bool, str]:
        if mode == "sandbox":
            db_path = self._database_path(mode)
            existed_before = db_path.exists()
            self._start_sandbox_core()
        else:
            self._ensure_normal_core_ready(_state)
            db_path = self._database_path(mode)
            existed_before = db_path.exists()
        try:
            self._validate_database(db_path)
        except Exception as exc:
            failure = exc if isinstance(exc, InitializationFailure) else InitializationFailure(
                "DATABASE_INITIALIZATION_FAILED",
                "本地数据库检查失败",
            )
            if mode != "normal" or not self._request_core_repair_and_wait(
                _state,
                "检测到记忆库迁移或读写异常，正在重启核心服务后复检",
            ):
                if isinstance(exc, InitializationFailure):
                    raise
                raise failure from exc
            try:
                self._validate_database(db_path)
            except InitializationFailure:
                raise
            except Exception as retry_exc:
                raise InitializationFailure(
                    "DATABASE_INITIALIZATION_FAILED",
                    "本地数据库自动复检失败",
                ) from retry_exc
        if existed_before:
            return True, "本地记忆库已存在，迁移与读写检查通过"
        return False, "隔离记忆库已创建，迁移与读写检查通过"

    def _ensure_normal_core_ready(self, state: dict[str, Any]) -> None:
        if self._core_healthy():
            return
        self._set_recovery_state(
            state,
            status="waiting",
            action="wait_for_core_service",
            attempt=0,
            max_attempts=1,
            error_code="DATABASE_INITIALIZATION_FAILED",
            message="本地核心服务仍在启动，正在自动等待",
        )
        if self._wait_for_core_health(CORE_STARTUP_GRACE_SECONDS):
            self._set_recovery_state(
                self._load_state("normal"),
                status="succeeded",
                action="wait_for_core_service",
                attempt=0,
                max_attempts=1,
                error_code=None,
                message="本地核心服务已就绪，继续初始化",
            )
            return
        if self._request_core_repair_and_wait(
            self._load_state("normal"),
            "本地核心服务未响应，正在安全重启应用内置服务",
        ):
            return
        code = "CORE_PORT_CONFLICT" if self._port_in_use(7070) else "CORE_SERVICE_UNAVAILABLE"
        message = "本地服务端口 7070 被其他程序占用" if code == "CORE_PORT_CONFLICT" else "本地核心服务自动重启后仍未就绪"
        raise InitializationFailure(code, message)

    def _request_core_repair_and_wait(self, state: dict[str, Any], message: str) -> bool:
        if os.environ.get("MEMORY_BREAD_PACKAGED") != "1":
            logger.info("core service host repair is unavailable outside packaged runtime")
            return False
        request_path = self.base_dir / "state" / "backend-repair-core.request"
        self._write_json_atomic(
            request_path,
            {
                "service": "core",
                "requested_at": _utc_now(),
                "run_id": state.get("run_id"),
            },
        )
        self._set_recovery_state(
            state,
            status="running",
            action="restart_core_service",
            attempt=1,
            max_attempts=1,
            error_code="DATABASE_INITIALIZATION_FAILED",
            message=message,
        )
        repaired = self._wait_for_backend_repair(
            request_path,
            CORE_REPAIR_TIMEOUT_SECONDS,
        )
        latest = self._load_state("normal")
        self._set_recovery_state(
            latest,
            status="succeeded" if repaired else "exhausted",
            action="restart_core_service",
            attempt=1,
            max_attempts=1,
            error_code=None if repaired else "DATABASE_INITIALIZATION_FAILED",
            message="本地核心服务已自动恢复" if repaired else "本地核心服务自动修复未完成",
        )
        logger.info(
            "core_service_auto_repair result=%s run_id=%s",
            "succeeded" if repaired else "exhausted",
            state.get("run_id"),
        )
        return repaired

    def _wait_for_backend_repair(self, request_path: Path, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        acknowledged = False
        while time.monotonic() < deadline:
            if not request_path.exists():
                acknowledged = True
            if acknowledged and self._core_healthy():
                return True
            time.sleep(0.5)
        return False

    def _wait_for_core_health(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._core_healthy():
                return True
            time.sleep(0.5)
        return False

    def _stage_skills_tools(self, mode: str, _state: dict[str, Any]) -> tuple[bool, str]:
        try:
            from creation.tools import REQUIRED_CREATION_TOOL_IDS

            required_tools = tuple(REQUIRED_CREATION_TOOL_IDS)
        except Exception as exc:
            raise InitializationFailure(
                "SKILLS_TOOLS_INITIALIZATION_FAILED",
                f"内置工具清单加载失败: {exc}",
            ) from exc
        if not required_tools or any(not isinstance(item, str) or not item for item in required_tools):
            raise InitializationFailure("SKILLS_TOOLS_INITIALIZATION_FAILED", "内置工具清单不完整")
        db_path = self._database_path(mode)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creation_skills'"
            ).fetchone()
        if not row:
            raise InitializationFailure("SKILLS_TOOLS_INITIALIZATION_FAILED", "内置 Skill 存储尚未初始化")
        manifest_path = self._workspace_root(mode) / "skills-tools.json"
        expected_manifest = {
            "schema_version": 1,
            "required_tools": sorted(required_tools),
        }
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = None
        if existing_manifest == expected_manifest:
            return True, "内置技能与工具已初始化，清单检查通过"
        self._write_json_atomic(manifest_path, expected_manifest)
        return False, "内置技能与工具已初始化并通过清单检查"

    def _stage_quality_gate(self, mode: str, state: dict[str, Any]) -> tuple[bool, str]:
        checks = []
        base_url = self._ollama_base_url(mode)
        engine_ready = self._ollama_healthy(base_url) and (
            mode != "sandbox" or self._sandbox_process_owned("ollama")
        )
        checks.append(self._quality_check("engine_health", engine_ready))
        installed = self._installed_model_names(base_url)
        checks.append(self._quality_check("capture_model_ready", self._model_present(installed, _CAPTURE_MODEL_NAME)))
        checks.append(self._quality_check("vector_model_ready", self._model_present(installed, _VECTOR_MODEL_NAME)))
        db_path = self._database_path(mode)
        try:
            self._validate_database(db_path)
            checks.append(self._quality_check("database_integrity", True))
        except Exception:
            checks.append(self._quality_check("database_integrity", False, "DATABASE_INITIALIZATION_FAILED"))
        passed = all(item["status"] == "passed" for item in checks)
        state["quality_gate"] = {"passed": passed, "checks": checks}
        self._save_state(state)
        if not passed:
            raise InitializationFailure("QUALITY_GATE_FAILED", "至少一个组件质检未通过")
        return False, "全部组件质检通过"

    def _stage_feature_smoke_tests(self, mode: str, state: dict[str, Any]) -> tuple[bool, str]:
        tests: list[dict[str, Any]] = []
        probes: tuple[tuple[str, Callable[[str], None]], ...] = (
            ("capture_probe", self._probe_capture),
            ("extraction_probe", self._probe_extraction),
            ("consultation_probe", self._probe_consultation),
            ("creation_probe", self._probe_creation),
        )
        for test_id, probe in probes:
            started = time.monotonic()
            try:
                probe(mode)
                tests.append(
                    {
                        "id": test_id,
                        "status": "passed",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error_code": None,
                    }
                )
            except Exception as exc:
                logger.warning("initialization smoke test failed test=%s error=%s", test_id, exc)
                tests.append(
                    {
                        "id": test_id,
                        "status": "failed",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error_code": "FEATURE_SMOKE_TEST_FAILED",
                    }
                )
                state["smoke_tests"] = tests
                self._save_state(state)
                raise InitializationFailure(
                    "FEATURE_SMOKE_TEST_FAILED",
                    f"核心功能测试未通过: {test_id}",
                ) from exc
        state["smoke_tests"] = tests
        self._save_state(state)
        return False, "采集、提炼、咨询和创作测试全部通过"

    # ── managed runtime ───────────────────────────────────────────────────

    @staticmethod
    def _ollama_download_candidates() -> list[str]:
        """构造运行时下载候选源列表（境内加速源优先）。

        顺序：显式覆盖地址 -> 自建镜像 -> 内置加速代理 -> GitHub 官方。
        所有候选产物统一做 SHA256 校验，任一候选成功即止。
        """
        candidates: list[str] = []
        override = os.environ.get("MEMORY_BREAD_OLLAMA_DOWNLOAD_URL", "").strip()
        if override:
            candidates.append(override)
        extra = os.environ.get("MEMORY_BREAD_OLLAMA_DOWNLOAD_MIRRORS", "").strip()
        if extra:
            candidates.extend(u.strip() for u in extra.split(",") if u.strip())
        for prefix in MANAGED_OLLAMA_MIRROR_PREFIXES:
            candidates.append(prefix + MANAGED_OLLAMA_URL)
        if not override:
            candidates.append(MANAGED_OLLAMA_URL)
        return candidates

    def _download_archive_with_resume(self, url: str, archive: Path) -> None:
        """单个候选源的下载（支持断点续传，单源内重试 3 次）。"""
        last_error: Optional[Exception] = None
        for attempt in range(3):
            downloaded = archive.stat().st_size if archive.exists() else 0
            headers = {"User-Agent": "MemoryBread-Initializer/1"}
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    resumed = downloaded > 0 and getattr(response, "status", None) == 206
                    if not resumed:
                        downloaded = 0
                    content_length = int(response.headers.get("Content-Length") or 0)
                    total = downloaded + content_length if content_length > 0 else 0
                    with open(archive, "ab" if resumed else "wb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                self._update_stage_download_progress(
                                    "inference_engine",
                                    min(80, int(downloaded * 80 / total)),
                                    "正在下载本地 AI 引擎",
                                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        assert last_error is not None
        raise last_error

    def _install_managed_ollama(self, mode: str) -> Path:
        root = self._runtime_root(mode)
        version_dir = root / f"v{MANAGED_OLLAMA_VERSION}"
        version_dir.mkdir(parents=True, exist_ok=True)
        archive = version_dir / "ollama-darwin.tgz.part"
        expected_sha = os.environ.get("MEMORY_BREAD_OLLAMA_SHA256", MANAGED_OLLAMA_SHA256).lower()

        # 逐候选源尝试：境内加速源优先，全部失败才报下载错误。
        # 切换候选源时丢弃旧的部分文件，避免不同源之间断点不兼容。
        last_error: Optional[Exception] = None
        for url in self._ollama_download_candidates():
            try:
                logger.info("尝试从 %s 下载本地 AI 引擎", url)
                self._download_archive_with_resume(url, archive)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                logger.warning("本地 AI 引擎下载失败，切换下一个候选源: %s (%s)", url, exc)
                archive.unlink(missing_ok=True)
        if last_error is not None:
            raise InitializationFailure(
                "RUNTIME_DOWNLOAD_FAILED",
                f"本地 AI 引擎下载失败: {last_error}",
            ) from last_error

        actual_sha = self._sha256_file(archive)
        if actual_sha != expected_sha:
            archive.unlink(missing_ok=True)
            raise InitializationFailure("RUNTIME_CHECKSUM_MISMATCH", "本地 AI 引擎文件校验失败")

        extract_dir = Path(tempfile.mkdtemp(prefix="extract-", dir=version_dir))
        try:
            with tarfile.open(archive, "r:gz") as tar:
                self._safe_extract_tar(tar, extract_dir)
            executable = self._find_ollama_executable(extract_dir)
            if executable is None:
                raise InitializationFailure("RUNTIME_DOWNLOAD_FAILED", "下载包中缺少本地 AI 引擎")
            final_dir = version_dir / "runtime"
            if final_dir.exists():
                shutil.rmtree(final_dir)
            os.replace(extract_dir, final_dir)
            archive.unlink(missing_ok=True)
            executable = self._find_ollama_executable(final_dir)
            if executable is None:
                raise InitializationFailure("RUNTIME_DOWNLOAD_FAILED", "本地 AI 引擎安装不完整")
            executable.chmod(executable.stat().st_mode | 0o111)
            self._write_json_atomic(
                version_dir / "manifest.json",
                {
                    "version": MANAGED_OLLAMA_VERSION,
                    "archive_sha256": actual_sha,
                    "installed_at": _utc_now(),
                },
            )
            return executable
        finally:
            archive.unlink(missing_ok=True)
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)

    def _start_ollama(self, mode: str, executable: Path) -> None:
        port = self._ollama_port(mode)
        if self._port_in_use(port):
            if (
                mode == "sandbox"
                and self._ollama_healthy(self._ollama_base_url(mode))
                and self._sandbox_process_owned("ollama")
            ):
                return
            code = "SANDBOX_ISOLATION_FAILED" if mode == "sandbox" else "RUNTIME_START_FAILED"
            raise InitializationFailure(code, f"本地端口 {port} 已被其他服务占用")
        models_dir = self._models_root(mode)
        models_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._workspace_root(mode) / "logs" / "local-ai-engine.log"
        env = self._process_environment(mode)
        env.update(
            {
                "OLLAMA_HOST": f"127.0.0.1:{port}",
                "OLLAMA_MODELS": str(models_dir),
                "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_NOHISTORY": "1",
                # 全局最多驻留 1 个模型 = 最多 1 个 llama-server 子进程。
                "OLLAMA_MAX_LOADED_MODELS": "1",
            }
        )
        try:
            process = self._spawn_logged_process(
                [str(executable), "serve"],
                env=env,
                log_path=log_path,
            )
        except Exception as exc:
            raise InitializationFailure("RUNTIME_START_FAILED", f"本地 AI 引擎启动失败: {exc}") from exc
        self._processes[f"ollama:{mode}"] = process
        self._write_process_marker(
            mode,
            "ollama",
            process.pid,
            executable,
            {"port": port, "models_root": str(models_dir.resolve())},
        )

    def _ensure_model(self, mode: str, model_name: str, label: str) -> tuple[bool, str]:
        base_url = self._ollama_base_url(mode)
        installed = self._installed_model_names(base_url)
        if self._model_present(installed, model_name):
            return True, f"{label}已就绪，已跳过下载"
        data = json.dumps({"name": model_name, "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/api/pull",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        stage_id = "capture_model" if model_name == _CAPTURE_MODEL_NAME else "vector_model"
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=3600) as response:
                    for raw_line in response:
                        if not raw_line.strip():
                            continue
                        item = json.loads(raw_line.decode("utf-8"))
                        if item.get("error"):
                            raise RuntimeError(str(item["error"]))
                        total = int(item.get("total") or 0)
                        completed = int(item.get("completed") or 0)
                        if total > 0:
                            self._update_stage_download_progress(
                                stage_id,
                                min(99, int(completed * 100 / total)),
                                f"正在下载{label}",
                            )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
                    request = urllib.request.Request(
                        f"{base_url}/api/pull",
                        data=data,
                        headers={"Content-Type": "application/json"},
                    )
        if last_error is not None:
            raise InitializationFailure(
                "MODEL_DOWNLOAD_FAILED",
                f"{label}下载失败: {last_error}",
            ) from last_error
        if not self._model_present(self._installed_model_names(base_url), model_name):
            raise InitializationFailure("MODEL_DOWNLOAD_FAILED", f"{label}下载后校验失败")
        return False, f"{label}已下载并通过校验"

    # ── database, checks and probes ───────────────────────────────────────

    def _validate_database(self, db_path: Path) -> None:
        deadline = time.monotonic() + 20
        while not db_path.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        if not db_path.exists():
            raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", "本地数据库尚未创建")
        required_tables = {"schema_migrations", "captures", "timelines", "creation_skills"}
        with sqlite3.connect(db_path, timeout=10) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", "数据库完整性检查失败")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            missing = sorted(required_tables - tables)
            if missing:
                raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", "数据库迁移尚未完成")
            conn.execute("CREATE TEMP TABLE initialization_probe(value TEXT NOT NULL)")
            conn.execute("INSERT INTO initialization_probe(value) VALUES ('ok')")
            if conn.execute("SELECT value FROM initialization_probe").fetchone()[0] != "ok":
                raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", "数据库读写探针失败")

    def _start_sandbox_core(self) -> None:
        core_port = self._core_port("sandbox")
        base_url = f"http://127.0.0.1:{core_port}"
        if self._http_ok(f"{base_url}/health"):
            if self._sandbox_process_owned("core"):
                return
            raise InitializationFailure(
                "SANDBOX_ISOLATION_FAILED",
                "隔离核心服务端口被非本次沙箱的进程占用",
            )
        if self._port_in_use(core_port):
            raise InitializationFailure(
                "SANDBOX_ISOLATION_FAILED",
                "隔离核心服务端口已被其他进程占用",
            )
        executable = self._resolve_core_executable()
        if executable is None:
            raise InitializationFailure("SANDBOX_CORE_UNAVAILABLE", "未找到隔离测试所需的核心服务")
        home = self.sandbox_root / "home"
        home.mkdir(parents=True, exist_ok=True)
        log_path = self.sandbox_root / "logs" / "core.log"
        env = self._process_environment("sandbox")
        env.update(
            {
                "HOME": str(home),
                "MEMORY_BREAD_CORE_BIND": f"127.0.0.1:{core_port}",
                "MEMORY_BREAD_CAPTURE_ENABLED": "0",
            }
        )
        try:
            process = self._spawn_logged_process(
                [str(executable)],
                env=env,
                log_path=log_path,
            )
        except Exception as exc:
            raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", f"隔离核心服务启动失败: {exc}") from exc
        self._processes["core:sandbox"] = process
        self._write_process_marker(
            "sandbox",
            "core",
            process.pid,
            executable,
            {"port": core_port, "database": str(self._database_path("sandbox").resolve())},
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._http_ok(f"{base_url}/health"):
                return
            if process.poll() is not None:
                break
            time.sleep(0.5)
        raise InitializationFailure("DATABASE_INITIALIZATION_FAILED", "隔离核心服务启动超时")

    def _probe_capture(self, mode: str) -> None:
        db_path = self._database_path(mode)
        capture_id: Optional[int] = None
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                cursor = conn.execute(
                    "INSERT INTO captures "
                    "(ts, app_name, event_type, ax_text, is_sensitive, pii_scrubbed) "
                    "VALUES (?, ?, 'manual', ?, 1, 1)",
                    (
                        int(time.time() * 1000),
                        "MemoryBread 初始化质检",
                        "MemoryBread initialization synthetic probe",
                    ),
                )
                capture_id = int(cursor.lastrowid)
            core_port = self._core_port(mode)
            data = self._http_json(
                f"http://127.0.0.1:{core_port}/api/captures?ids={capture_id}",
                timeout=10,
            )
            captures = data.get("captures", [])
            if not any(int(item.get("id", 0)) == capture_id for item in captures):
                raise RuntimeError("capture API probe failed")
        finally:
            if capture_id is not None:
                with sqlite3.connect(db_path, timeout=10) as conn:
                    conn.execute("DELETE FROM captures WHERE id = ?", (capture_id,))

    def _probe_extraction(self, mode: str) -> None:
        response = self._ollama_generate(
            mode,
            "请把这句话概括为不超过八个字：今天完成初始化测试。",
        )
        if not response.strip():
            raise RuntimeError("empty extraction result")

    def _probe_consultation(self, mode: str) -> None:
        base_url = self._ollama_base_url(mode)
        payload = json.dumps(
            {"model": _VECTOR_MODEL_NAME, "input": "初始化向量探针"}
        ).encode("utf-8")
        try:
            data = self._http_json(f"{base_url}/api/embed", payload)
        except Exception:
            fallback = json.dumps(
                {"model": _VECTOR_MODEL_NAME, "prompt": "初始化向量探针"}
            ).encode("utf-8")
            data = self._http_json(f"{base_url}/api/embeddings", fallback)
        embeddings = data.get("embeddings") or ([data.get("embedding")] if data.get("embedding") else [])
        if not embeddings or not embeddings[0]:
            raise RuntimeError("empty embedding")
        answer = self._ollama_generate(mode, "只回答“正常”：1 加 1 是否等于 2？")
        if not answer.strip():
            raise RuntimeError("empty consultation result")

    def _probe_creation(self, mode: str) -> None:
        from creation.tools import REQUIRED_CREATION_TOOL_IDS

        if not tuple(REQUIRED_CREATION_TOOL_IDS):
            raise RuntimeError("creation tools unavailable")
        response = self._ollama_generate(mode, "写一句不超过二十字的初始化完成提示。")
        if not response.strip():
            raise RuntimeError("empty creation result")

    def _ollama_generate(self, mode: str, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": _CAPTURE_MODEL_NAME,
                "prompt": prompt,
                "stream": False,
                # 初始化探针只验证基础生成能力。关闭长思考并限制上下文/
                # 输出，避免新安装模型在首次加载后为固定短问题持续推理
                # 数分钟，造成可用组件被误报为质检超时。
                "think": False,
                "keep_alive": 0,
                "options": {
                    "num_ctx": 2048,
                    "num_predict": 64,
                    "temperature": 0,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        data = self._http_json(f"{self._ollama_base_url(mode)}/api/generate", payload, timeout=180)
        return str(data.get("response") or "")

    # ── helpers ───────────────────────────────────────────────────────────

    def _new_state(self, mode: str) -> dict[str, Any]:
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(uuid.uuid4()),
            "mode": mode,
            "state": "running",
            "progress": 0,
            "current_stage": "preflight",
            "message": "准备初始化",
            "suggestion": None,
            "error_code": None,
            "stages": [
                {
                    "id": stage_id,
                    "label": label,
                    "status": "pending",
                    "progress": 0,
                    "detail": "等待执行",
                    "error_code": None,
                    "duration_ms": None,
                }
                for stage_id, label, _start, _end in STAGES
            ],
            "quality_gate": {"passed": False, "checks": []},
            "smoke_tests": [],
            "can_retry": False,
            "can_report": False,
            "started_at": _utc_now(),
            "finished_at": None,
            "recovery": None,
        }
        if mode == "sandbox":
            cold_start = self._sandbox_artifacts_absent()
            state["sandbox_cold_start"] = cold_start
            state["sandbox_isolation"] = self._sandbox_isolation_status(cold_start)
        return state

    def _empty_state(self, mode: str) -> dict[str, Any]:
        state = self._new_state(mode)
        state.update(
            {
                "run_id": None,
                "state": "not_started",
                "message": "需要完成初始化后才能使用记忆面包",
                "started_at": None,
            }
        )
        return state

    def _load_active_state(self) -> dict[str, Any]:
        return self._load_state("sandbox" if self._test_mode_enabled() else "normal")

    def _refresh_completed_state(self, state: dict[str, Any]) -> dict[str, Any]:
        mode = str(state.get("mode") or "normal")
        if (
            state.get("state") == "interrupted"
            and state.get("error_code") == "INITIALIZATION_COMPONENT_MISSING"
        ):
            # 此前因暂时性原因（如启动早期引擎尚未就绪）被降级的状态，
            # 在组件恢复可用后应自动回到完成状态，而不是永远停在恢复页。
            healed = self._recover_interrupted_state(state, mode)
            if healed is not None:
                return healed
            return state
        if (
            state.get("state") != "completed"
            or not state.get("quality_gate", {}).get("passed")
            or self._completed_state_still_valid(mode)
        ):
            self._invalid_since[mode] = None
            return state
        if not self._components_genuinely_missing(mode):
            # 模型文件与记忆库仍然完好，只是引擎暂时不可达。给启动时序竞态
            # 一个宽限窗口，窗口内恢复则保持完成状态；不提前持久化降级。
            now = time.monotonic()
            since = self._invalid_since.get(mode)
            if since is None:
                self._invalid_since[mode] = now
                logger.info(
                    "completed state recheck failed, entering grace window mode=%s",
                    mode,
                )
                return state
            if now - since < INVALID_STATE_GRACE_SECONDS:
                return state
        self._invalid_since[mode] = None
        invalid = self._empty_state(mode)
        invalid.update(
            {
                "state": "interrupted",
                "message": "检测到本地能力需要恢复",
                "error_code": "INITIALIZATION_COMPONENT_MISSING",
                "suggestion": ERROR_SUGGESTIONS["INITIALIZATION_COMPONENT_MISSING"],
                "can_retry": True,
                "can_report": False,
                "finished_at": _utc_now(),
            }
        )
        self._save_state(invalid)
        logger.warning(
            "completed state demoted to interrupted mode=%s error_code=%s",
            mode,
            "INITIALIZATION_COMPONENT_MISSING",
        )
        return invalid

    def _recover_interrupted_state(
        self, state: dict[str, Any], mode: str
    ) -> Optional[dict[str, Any]]:
        try:
            if not self._completed_state_still_valid(mode):
                return None
        except Exception:
            return None
        restored = copy.deepcopy(state)
        restored.update(
            {
                "state": "completed",
                "progress": 100,
                "current_stage": "feature_smoke_tests",
                "message": "初始化与质检已完成",
                "suggestion": None,
                "error_code": None,
                "can_retry": False,
                "can_report": False,
                "finished_at": _utc_now(),
            }
        )
        restored.setdefault("quality_gate", {"passed": False, "checks": []})
        restored["quality_gate"]["passed"] = True
        for stage in restored.get("stages", []):
            stage.update(
                {
                    "status": "succeeded",
                    "progress": 100,
                    "detail": "已恢复，复用既有组件",
                    "error_code": None,
                }
            )
        self._save_state(restored)
        logger.info("interrupted state recovered mode=%s", mode)
        return restored

    def _components_genuinely_missing(self, mode: str) -> bool:
        """只做不依赖引擎运行状态的磁盘级组件检查。

        引擎本身由应用在启动过程中拉起，sidecar 首次健康检查时它可能还未
        就绪，因此引擎不可达不视为组件缺失，由宽限窗口处理。
        """
        try:
            manifests_dir = self._models_root(mode) / "manifests" / "registry.ollama.ai"
            if not manifests_dir.exists() or not any(manifests_dir.rglob("*")):
                return True
            self._validate_database(self._database_path(mode))
            return False
        except Exception:
            return True

    def _completed_state_still_valid(self, mode: str) -> bool:
        try:
            base_url = self._ollama_base_url(mode)
            if not self._ollama_healthy(base_url):
                return False
            if mode == "normal" and self._ollama_gui_running():
                return False
            installed = self._installed_model_names(base_url)
            if not self._model_present(installed, _CAPTURE_MODEL_NAME):
                return False
            if not self._model_present(installed, _VECTOR_MODEL_NAME):
                return False
            if mode == "sandbox" and not self._sandbox_process_owned("ollama"):
                return False
            db_path = self._database_path(mode)
            self._validate_database(db_path)
            return True
        except Exception:
            return False

    def _load_state(self, mode: str) -> dict[str, Any]:
        path = self._state_path(mode)
        if not path.exists():
            return self._empty_state(mode)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != SCHEMA_VERSION:
                return self._empty_state(mode)
            return data
        except Exception:
            logger.warning("initialization state unreadable path=%s", path)
            return self._empty_state(mode)

    def _save_state(self, state: dict[str, Any]) -> None:
        self._write_json_atomic(self._state_path(state["mode"]), state)

    def _state_path(self, mode: str) -> Path:
        return self.sandbox_root / "state.json" if mode == "sandbox" else self.normal_state_path

    def _test_mode_enabled(self) -> bool:
        try:
            return bool(json.loads(self.mode_path.read_text(encoding="utf-8")).get("enabled"))
        except Exception:
            return False

    def _mark_interrupted_runs(self) -> None:
        for mode in ("normal", "sandbox"):
            state = self._load_state(mode)
            if state.get("state") == "running":
                state.update(
                    {
                        "state": "interrupted",
                        "message": "上次初始化被中断，可以继续",
                        "error_code": "INITIALIZATION_INTERRUPTED",
                        "suggestion": "点击继续初始化，已完成内容会自动跳过。",
                        "can_retry": True,
                        "can_report": False,
                        "finished_at": _utc_now(),
                    }
                )
                self._save_state(state)

    def _public_state(self, state: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "schema_version",
            "run_id",
            "mode",
            "state",
            "progress",
            "current_stage",
            "message",
            "suggestion",
            "error_code",
            "stages",
            "quality_gate",
            "smoke_tests",
            "can_retry",
            "can_report",
            "started_at",
            "finished_at",
            "test_mode_enabled",
            "sandbox_isolation",
            "recovery",
        }
        return {key: copy.deepcopy(value) for key, value in state.items() if key in allowed}

    def _update_stage_download_progress(self, stage_id: str, progress: int, detail: str) -> None:
        with self._lock:
            state = self._load_active_state()
            stage_bounds = next((item for item in STAGES if item[0] == stage_id), None)
            if stage_bounds is None:
                return
            _id, _label, start, end = stage_bounds
            state["progress"] = max(
                int(state.get("progress", 0)),
                start + int((end - start) * max(0, min(100, progress)) / 100),
            )
            state["message"] = detail
            for stage in state["stages"]:
                if stage["id"] == stage_id:
                    stage["progress"] = max(int(stage.get("progress", 0)), progress)
                    stage["detail"] = detail
            self._save_state(state)

    def _sandbox_mode_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.mode_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _available_loopback_port(excluded: set[int]) -> int:
        for _attempt in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            if port not in excluded:
                return port
        raise InitializationFailure("SANDBOX_ISOLATION_FAILED", "无法为隔离环境分配本地端口")

    def _allocate_sandbox_ports(self) -> tuple[int, int]:
        excluded = {NORMAL_OLLAMA_PORT, 7070, 7071, 7072, 8001}
        ollama_port = self._available_loopback_port(excluded)
        excluded.add(ollama_port)
        core_port = self._available_loopback_port(excluded)
        return ollama_port, core_port

    def _sandbox_port(self, key: str, fallback: int) -> int:
        value = self._sandbox_mode_config().get(key)
        try:
            port = int(value)
        except (TypeError, ValueError):
            return fallback
        return port if 1024 <= port <= 65535 else fallback

    def _ollama_port(self, mode: str) -> int:
        if mode == "sandbox":
            return self._sandbox_port("ollama_port", SANDBOX_OLLAMA_PORT)
        return NORMAL_OLLAMA_PORT

    def _core_port(self, mode: str) -> int:
        if mode == "sandbox":
            return self._sandbox_port("core_port", SANDBOX_CORE_PORT)
        return 7070

    def _database_path(self, mode: str) -> Path:
        if mode == "sandbox":
            return self.sandbox_root / "home" / ".memory-bread" / "memory-bread.db"
        return self.base_dir / "memory-bread.db"

    def _process_environment(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        if mode != "sandbox":
            return env
        for key in (
            "OLLAMA_HOST",
            "OLLAMA_MODELS",
            "OLLAMA_ORIGINS",
            "MEMORY_BREAD_CORE_BIND",
            "MEMORY_BREAD_CAPTURE_ENABLED",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "TMPDIR",
        ):
            env.pop(key, None)
        home = self.sandbox_root / "home"
        config = self.sandbox_root / "xdg" / "config"
        cache = self.sandbox_root / "xdg" / "cache"
        data = self.sandbox_root / "xdg" / "data"
        temporary = self.sandbox_root / "tmp"
        for path in (home, config, cache, data, temporary):
            path.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(cache),
                "XDG_DATA_HOME": str(data),
                "TMPDIR": str(temporary),
                "MEMORY_BREAD_INITIALIZATION_SANDBOX": "1",
            }
        )
        return env

    def _sandbox_artifacts_absent(self) -> bool:
        runtime_root = self._runtime_root("sandbox")
        models_root = self._models_root("sandbox")
        skills_manifest = self._workspace_root("sandbox") / "skills-tools.json"
        return (
            not runtime_root.exists()
            and not models_root.exists()
            and not self._database_path("sandbox").exists()
            and not skills_manifest.exists()
            and not self._port_in_use(self._ollama_port("sandbox"))
            and not self._port_in_use(self._core_port("sandbox"))
        )

    @staticmethod
    def _sandbox_isolation_status(cold_start: bool) -> dict[str, bool]:
        return {
            "enforced": True,
            "cold_start": cold_start,
            "normal_runtime_hidden": True,
            "normal_models_hidden": True,
            "normal_database_hidden": True,
        }

    def _workspace_root(self, mode: str) -> Path:
        return self.sandbox_root if mode == "sandbox" else self.base_dir / "initialization"

    def _runtime_root(self, mode: str) -> Path:
        return self._workspace_root(mode) / "runtime" / "ollama"

    def _models_root(self, mode: str) -> Path:
        return self._workspace_root(mode) / "models"

    def _managed_ollama_executable(self, mode: str) -> Optional[Path]:
        version_dir = self._runtime_root(mode) / f"v{MANAGED_OLLAMA_VERSION}"
        manifest_path = version_dir / "manifest.json"
        expected_sha = os.environ.get("MEMORY_BREAD_OLLAMA_SHA256", MANAGED_OLLAMA_SHA256).lower()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("version") != MANAGED_OLLAMA_VERSION
                or manifest.get("archive_sha256") != expected_sha
            ):
                return None
        except Exception:
            return None
        return self._find_ollama_executable(version_dir / "runtime")

    def _managed_ollama_process_owned(self, mode: str) -> bool:
        marker_path = self._workspace_root(mode) / "processes" / "ollama.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if mode == "sandbox":
                sandbox_id = self._sandbox_mode_config().get("sandbox_id")
                if not sandbox_id or marker.get("sandbox_id") != sandbox_id:
                    return False
            if int(marker.get("port")) != self._ollama_port(mode):
                return False
            executable = Path(marker["executable"]).resolve()
            runtime_root = self._runtime_root(mode).resolve()
            if runtime_root != executable and runtime_root not in executable.parents:
                return False
            if Path(marker.get("models_root", "")).resolve() != self._models_root(mode).resolve():
                return False
            process = psutil.Process(int(marker["pid"]))
            create_time = marker.get("create_time")
            if create_time is None or abs(process.create_time() - float(create_time)) > 0.01:
                return False
            return process.is_running() and self._process_matches_marker(process, executable)
        except Exception:
            return False

    @staticmethod
    def _ollama_gui_processes() -> list[psutil.Process]:
        processes: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                raw_paths = [process.info.get("exe")]
                raw_paths.extend((process.info.get("cmdline") or [])[:2])
                for raw_path in raw_paths:
                    if not raw_path or not str(raw_path).startswith("/"):
                        continue
                    candidate = Path(str(raw_path)).resolve()
                    if candidate == OLLAMA_GUI_APP_ROOT or OLLAMA_GUI_APP_ROOT in candidate.parents:
                        processes.append(process)
                        break
            except (OSError, psutil.Error):
                continue
        return processes

    def _ollama_gui_running(self) -> bool:
        return bool(self._ollama_gui_processes())

    def _stop_ollama_gui(self) -> None:
        processes = self._ollama_gui_processes()
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                raise InitializationFailure(
                    "RUNTIME_START_FAILED",
                    "旧本地 AI 引擎无法退出",
                ) from exc
        _gone, alive = psutil.wait_procs(processes, timeout=8)
        if alive:
            raise InitializationFailure(
                "RUNTIME_START_FAILED",
                "旧本地 AI 引擎仍在运行",
            )

    def _stop_owned_ollama_for_repair(self, mode: str) -> bool:
        """Stop only the managed process whose marker identity still matches."""
        if not self._managed_ollama_process_owned(mode):
            return False
        marker_path = self._workspace_root(mode) / "processes" / "ollama.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            process = psutil.Process(int(marker["pid"]))
            process.terminate()
            try:
                process.wait(timeout=8)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            marker_path.unlink(missing_ok=True)
            owned = self._processes.pop(f"ollama:{mode}", None)
            if owned is not None:
                try:
                    owned.wait(timeout=0.1)
                except Exception:
                    pass
            return True
        except (OSError, ValueError, KeyError, json.JSONDecodeError, psutil.Error):
            logger.warning("managed local AI process could not be stopped safely for repair")
            return False

    @staticmethod
    def _find_ollama_executable(root: Path) -> Optional[Path]:
        for candidate in (root / "bin" / "ollama", root / "ollama"):
            if candidate.is_file():
                return candidate
        for candidate in root.rglob("ollama") if root.exists() else ():
            if candidate.is_file() and candidate.name == "ollama":
                return candidate
        return None

    @staticmethod
    def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
        destination = destination.resolve()
        for member in tar.getmembers():
            member_path = (destination / member.name).resolve()
            if destination != member_path and destination not in member_path.parents:
                raise InitializationFailure("RUNTIME_CHECKSUM_MISMATCH", "运行时压缩包路径不安全")
            if member.issym() or member.islnk():
                link_path = (member_path.parent / member.linkname).resolve()
                if destination != link_path and destination not in link_path.parents:
                    raise InitializationFailure("RUNTIME_CHECKSUM_MISMATCH", "运行时压缩包链接不安全")
        tar.extractall(destination)

    def _ollama_base_url(self, mode: str) -> str:
        return f"http://127.0.0.1:{self._ollama_port(mode)}"

    def _ollama_healthy(self, base_url: str) -> bool:
        try:
            data = self._http_json(f"{base_url}/api/tags", timeout=2)
            return isinstance(data.get("models", []), list)
        except Exception:
            return False

    def _installed_model_names(self, base_url: str) -> list[str]:
        data = self._http_json(f"{base_url}/api/tags", timeout=10)
        return [
            str(item.get("model") or item.get("name") or "")
            for item in data.get("models", [])
        ]

    @staticmethod
    def _model_present(installed: list[str], expected: str) -> bool:
        expected_base = expected.split(":")[0]
        return any(item == expected or item.split(":")[0] == expected_base for item in installed)

    @staticmethod
    def _http_json(url: str, payload: Optional[bytes] = None, timeout: int = 30) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _http_ok(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def _core_healthy(self) -> bool:
        try:
            payload = self._http_json("http://127.0.0.1:7070/health", timeout=2)
            return (
                payload.get("status") == "ok"
                and payload.get("service") == "memory-bread-core"
                and bool(payload.get("version"))
            )
        except Exception:
            return False

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _resolve_core_executable(self) -> Optional[Path]:
        configured = os.environ.get("MEMORY_BREAD_CORE_EXECUTABLE")
        candidates: list[Path] = [Path(configured)] if configured else []
        executable = Path(sys.executable).resolve()
        project_root = Path(__file__).resolve().parents[1]
        candidates.extend(
            [
                executable.parent / "memory-bread-core",
                executable.parents[4] / "MacOS" / "memory-bread-core"
                if len(executable.parents) > 4
                else executable,
                # 开发环境由 start.sh 构建并运行这里的 Core。必须优先于
                # Tauri target 中可能残留的旧打包副本，否则测试模式会用旧
                # migration 集合创建数据库，直到质检阶段才暴露版本错配。
                project_root / "core-engine" / "target" / "release" / "memory-bread",
                project_root
                / "core-engine"
                / "target"
                / "aarch64-apple-darwin"
                / "release"
                / "memory-bread",
                project_root
                / "core-engine"
                / "target"
                / "x86_64-apple-darwin"
                / "release"
                / "memory-bread",
                project_root
                / "desktop-ui"
                / "src-tauri"
                / "target"
                / "aarch64-apple-darwin"
                / "release"
                / "memory-bread-core",
                project_root
                / "desktop-ui"
                / "src-tauri"
                / "target"
                / "release"
                / "memory-bread-core",
            ]
        )
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    def _spawn_logged_process(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        log_path: Path,
    ) -> subprocess.Popen:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is not None:
            threading.Thread(
                target=self._pump_bounded_log,
                args=(process.stdout, log_path),
                daemon=True,
                name=f"bounded-log-{log_path.stem}",
            ).start()
        return process

    @staticmethod
    def _pump_bounded_log(stream: Any, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output = open(log_path, "ab")
        size = output.tell()
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if size + len(chunk) > MAX_PROCESS_LOG_BYTES:
                    output.close()
                    for index in range(PROCESS_LOG_BACKUPS, 0, -1):
                        source = log_path if index == 1 else log_path.with_name(f"{log_path.name}.{index - 1}")
                        destination = log_path.with_name(f"{log_path.name}.{index}")
                        if source.exists():
                            if destination.exists():
                                destination.unlink()
                            os.replace(source, destination)
                    output = open(log_path, "ab")
                    size = 0
                output.write(chunk)
                output.flush()
                size += len(chunk)
        except Exception:
            logger.debug("bounded process log pump stopped path=%s", log_path)
        finally:
            output.close()
            try:
                stream.close()
            except Exception:
                pass

    def _write_process_marker(
        self,
        mode: str,
        name: str,
        pid: int,
        executable: Path,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        marker_dir = self._workspace_root(mode) / "processes"
        try:
            create_time = psutil.Process(pid).create_time()
        except Exception:
            create_time = None
        marker = {
            "pid": pid,
            "executable": str(executable.resolve()),
            "create_time": create_time,
        }
        if mode == "sandbox":
            marker["sandbox_id"] = self._sandbox_mode_config().get("sandbox_id")
        if metadata:
            marker.update(metadata)
        self._write_json_atomic(
            marker_dir / f"{name}.json",
            marker,
        )

    @staticmethod
    def _process_matches_marker(process: psutil.Process, executable: Path) -> bool:
        command = process.cmdline()
        if not command:
            return False
        for part in command[:2]:
            try:
                if Path(part).resolve() == executable:
                    return True
            except Exception:
                continue
        return False

    def _sandbox_process_owned(self, name: str) -> bool:
        if name == "ollama":
            return self._managed_ollama_process_owned("sandbox")
        marker_path = self.sandbox_root / "processes" / f"{name}.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            config = self._sandbox_mode_config()
            if not config.get("sandbox_id") or marker.get("sandbox_id") != config.get("sandbox_id"):
                return False
            expected_port = self._ollama_port("sandbox") if name == "ollama" else self._core_port("sandbox")
            if int(marker.get("port")) != expected_port:
                return False
            executable = Path(marker["executable"]).resolve()
            if name == "ollama":
                runtime_root = self._runtime_root("sandbox").resolve()
                if runtime_root != executable and runtime_root not in executable.parents:
                    return False
                if Path(marker.get("models_root", "")).resolve() != self._models_root("sandbox").resolve():
                    return False
            elif name == "core":
                if Path(marker.get("database", "")).resolve() != self._database_path("sandbox").resolve():
                    return False
            process = psutil.Process(int(marker["pid"]))
            create_time = marker.get("create_time")
            if create_time is None or abs(process.create_time() - float(create_time)) > 0.01:
                return False
            return process.is_running() and self._process_matches_marker(process, executable)
        except Exception:
            return False

    def _stop_sandbox_processes(self) -> None:
        for key, process in list(self._processes.items()):
            if key.endswith(":sandbox") or key == "core:sandbox":
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                self._processes.pop(key, None)
        marker_dir = self.sandbox_root / "processes"
        if not marker_dir.exists():
            return
        for marker_path in marker_dir.glob("*.json"):
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                executable = Path(marker["executable"]).resolve()
                pid = int(marker["pid"])
                process = psutil.Process(pid)
                recorded_create_time = marker.get("create_time")
                if recorded_create_time is None or abs(process.create_time() - float(recorded_create_time)) > 0.01:
                    continue
                if self._process_matches_marker(process, executable):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except Exception:
                logger.debug("sandbox process marker cleanup skipped path=%s", marker_path)

    def _safe_remove_sandbox(self) -> None:
        target = self.sandbox_root.resolve()
        expected = (self.base_dir / "initialization-sandbox").resolve()
        if target != expected or target == self.base_dir:
            raise InitializationFailure("SANDBOX_CONFLICT", "沙箱目录校验失败")
        if target.exists():
            shutil.rmtree(target)

    def _environment_snapshot(self) -> dict[str, Any]:
        disk = psutil.disk_usage(str(self.base_dir if self.base_dir.exists() else Path.home()))
        return {
            "os": platform.system().lower(),
            "os_version": platform.mac_ver()[0] or platform.release(),
            "architecture": platform.machine().lower(),
            "memory_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "disk_free_gb": round(disk.free / (1024**3), 1),
        }

    def _installation_id(self) -> str:
        path = self.base_dir / "initialization" / "installation-id"
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass
        value = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return value

    @staticmethod
    def _notify_os_completion() -> None:
        """使用系统自带通知能力；失败不影响已经通过的初始化结果。"""
        if platform.system() != "Darwin":
            return
        try:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'display notification "本地 AI、记忆库与核心功能均已通过质检，可以开始使用。" '
                    'with title "记忆面包初始化完成"',
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            logger.debug("system initialization completion notification unavailable")

    @staticmethod
    def _component_report(stages: list[dict[str, Any]], stage_id: str, component_id: str) -> dict[str, Any]:
        stage = next((item for item in stages if item.get("id") == stage_id), {})
        return {
            "id": component_id,
            "status": stage.get("status", "pending"),
            "version": MANAGED_OLLAMA_VERSION if stage_id == "inference_engine" else None,
        }

    @staticmethod
    def _safe_summary(value: Any) -> Optional[str]:
        if not value:
            return None
        summary = InitializationManager._redact_internal_terms(str(value)).replace(
            str(Path.home()), "[home]"
        )
        summary = re.sub(r"(?<!:)(?<!\w)/(?:[^ \n\r\t]+)", "[path]", summary)
        return summary[:500]

    @staticmethod
    def _redact_internal_terms(value: str) -> str:
        redacted = value
        for internal_name, public_name in (
            (_CAPTURE_MODEL_NAME, "采集提炼模型"),
            (_VECTOR_MODEL_NAME, "向量模型"),
            ("Ollama", "本地 AI 引擎"),
            ("ollama", "本地 AI 引擎"),
        ):
            redacted = redacted.replace(internal_name, public_name)
        return redacted

    @staticmethod
    def _quality_check(check_id: str, passed: bool, error_code: Optional[str] = None) -> dict[str, Any]:
        return {
            "id": check_id,
            "status": "passed" if passed else "failed",
            "error_code": None if passed else error_code or "QUALITY_GATE_FAILED",
        }

    @staticmethod
    def _default_error_code(stage_id: str) -> str:
        return {
            "preflight": "UNSUPPORTED_PLATFORM",
            "inference_engine": "RUNTIME_START_FAILED",
            "capture_model": "MODEL_DOWNLOAD_FAILED",
            "vector_model": "MODEL_DOWNLOAD_FAILED",
            "database": "DATABASE_INITIALIZATION_FAILED",
            "skills_tools": "SKILLS_TOOLS_INITIALIZATION_FAILED",
            "quality_gate": "QUALITY_GATE_FAILED",
            "feature_smoke_tests": "FEATURE_SMOKE_TEST_FAILED",
        }.get(stage_id, "INITIALIZATION_FAILED")

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
